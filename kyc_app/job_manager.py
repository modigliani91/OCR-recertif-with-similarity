"""Exécution d'un traitement dans un thread d'arrière-plan, indépendant des sessions navigateur.

Pourquoi un thread : Streamlit interrompt le script d'une session lors d'une interaction ou
d'un rafraîchissement de page. Le pipeline (plusieurs heures possibles) ne doit pas dépendre
d'une session : il tourne dans un thread unique du processus de l'application (même processus
que le modèle, donc un seul exemplaire du modèle sur le GPU). Les sessions lisent seulement
l'instantané de progression.

Double protection contre deux traitements simultanés (double-clic, deux onglets, deux
utilisateurs) : verrou mémoire du processus + verrou fichier ``.kyc_processing.lock``.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import BinaryIO

from config import AppConfig
from exceptions import ConcurrentRunError, InvalidZipError
from file_manager import persist_upload, read_last_run, run_dir, write_run_metadata
from logging_config import get_logger
from pipeline import new_run_id, run_kyc_pipeline
from progress import CancelToken, ProgressTracker, TERMINAL_STATES, utc_now
from run_lock import RunLock

_SINGLETON: "JobManager | None" = None
_SINGLETON_LOCK = threading.Lock()


class JobManager:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.logger = get_logger("jobs")
        self._mutex = threading.Lock()
        self._thread: threading.Thread | None = None
        self._tracker: ProgressTracker | None = None
        self._cancel: CancelToken | None = None
        self._lock = RunLock(cfg.lock_path, cfg.lock_stale_after_s)
        self._heartbeat_stop = threading.Event()
        self._last_metadata: dict | None = None

    # -- état ---------------------------------------------------------------------------------
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict | None:
        tracker = self._tracker
        return tracker.snapshot() if tracker is not None else None

    def foreign_lock(self) -> dict | None:
        """Verrou actif détenu par un autre processus (ex. CLI dans un workspace partageant /mnt)."""
        if self.is_active():
            return None
        state = self._lock.status(local_job_active=False)
        return state.info if state.exists and not state.stale else None

    def last_run(self) -> dict | None:
        """Dernier résultat connu : mémoire du processus, sinon disque (survit aux redémarrages)."""
        if self._last_metadata is not None:
            return self._last_metadata
        metadata = read_last_run(self.cfg)
        if metadata and metadata.get("state") == "RUNNING" and not self.is_active() and not self.foreign_lock():
            metadata = {**metadata, "state": "INTERRUPTED",
                        "user_message": "Le traitement précédent a été interrompu (redémarrage de l'application). "
                                        "Relancez-le : les résultats déjà sauvegardés seront réutilisés."}
        return metadata

    # -- commandes ------------------------------------------------------------------------------
    def start(self, fileobj: BinaryIO, filename: str) -> str:
        with self._mutex:
            snapshot = self.snapshot()
            if self.is_active() and snapshot and snapshot["state"] in TERMINAL_STATES:
                self._thread.join(timeout=10)  # fin de thread imminente : état terminal déjà publié
            if self.is_active():
                raise ConcurrentRunError("Traitement déjà actif dans ce processus")
            run_id = new_run_id()
            self._lock.acquire(run_id, local_job_active=False)
            try:
                zip_path = persist_upload(fileobj, filename, self.cfg.input_dir)
            except InvalidZipError:
                self._lock.release()
                raise
            except Exception as exc:
                self._lock.release()
                raise InvalidZipError(str(exc), "L'archive n'a pas pu être enregistrée sur /mnt.") from exc
            self._cancel = CancelToken()
            self._tracker = ProgressTracker(run_id, run_dir(self.cfg, run_id) / "status.json")
            started = {"state": "RUNNING", "started_at": utc_now(), "input_zip": str(zip_path),
                       "input_filename": filename, "report_path": str(self.cfg.report_path),
                       "run_mode": self.cfg.run_mode}
            write_run_metadata(self.cfg, run_id, started)
            self._last_metadata = None
            self.logger.info("run=%s upload enregistré (%s) ; démarrage du traitement", run_id, zip_path.name)
            self._heartbeat_stop.clear()
            self._thread = threading.Thread(target=self._run, args=(run_id, zip_path, started),
                                            name=f"kyc-run-{run_id}", daemon=True)
            self._thread.start()
            threading.Thread(target=self._heartbeat_loop, name=f"kyc-heartbeat-{run_id}", daemon=True).start()
            return run_id

    def request_cancel(self) -> bool:
        if not self.is_active() or self._cancel is None:
            return False
        self._cancel.request()
        if self._tracker is not None:
            self._tracker.mark_cancel_requested()
        self.logger.warning("Arrêt demandé : appliqué avant le prochain document/client")
        return True

    # -- exécution -------------------------------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.cfg.heartbeat_interval_s):
            try:
                self._lock.heartbeat()
            except OSError:
                self.logger.exception("Battement du verrou impossible")

    def _run(self, run_id: str, zip_path, started: dict) -> None:
        tracker, result = self._tracker, None
        try:
            result = run_kyc_pipeline(zip_path, progress_callback=tracker, run_id=run_id,
                                      cancel_token=self._cancel, cfg=self.cfg)
            payload = result.to_dict()
            state = result.status
            error = None if result.success else {"kind": result.error_kind, "user_message": result.user_message}
        except BaseException as exc:  # garde-fou ultime : le pipeline ne devrait jamais lever ici
            self.logger.exception("run=%s erreur inattendue hors pipeline", run_id)
            payload, state = {"run_id": run_id, "status": "FAILED", "success": False,
                              "user_message": "Erreur inattendue. Consulter le journal de l'application."}, "FAILED"
            error = {"kind": type(exc).__name__, "user_message": payload["user_message"]}
        finally:
            self._heartbeat_stop.set()
        metadata = {**started, **payload, "state": state,
                    "finished_at": payload.get("finished_at") or utc_now(),
                    "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            write_run_metadata(self.cfg, run_id, metadata)
        except OSError:
            self.logger.exception("run=%s métadonnées de fin non écrites", run_id)
        finally:
            # Ordre : résultat mémorisé et verrou libéré AVANT que l'interface voie l'état terminal.
            self._last_metadata = metadata
            self._lock.release()
            tracker.finish(state if state in TERMINAL_STATES else "FAILED", result=payload, error=error)
            self.logger.info("run=%s verrou libéré (statut %s)", run_id, state)


def get_job_manager(cfg: AppConfig) -> JobManager:
    """Une seule instance par processus (également exposée à Streamlit via st.cache_resource)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = JobManager(cfg)
        return _SINGLETON
