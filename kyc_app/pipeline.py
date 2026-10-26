"""Orchestration d'un traitement KYC complet, sans Streamlit.

``run_kyc_pipeline`` enchaîne EXACTEMENT les cellules d'exécution du notebook, dans le même
ordre, via les fonctions générées :

    _nb_stage_inventory()                → cellule 11 (inventaire population)
    _nb_stage_pass_a()                   → cellule 22 (Pass A : RAW OCR + benchmark)
    _nb_stage_pass_b()                   → cellule 37 (Pass B : extraction structurée)
    _nb_stage_matching_and_reports()     → cellule 43 (référence, matching, rapports, audit)

Ajouts applicatifs (avant/après, jamais au milieu de la logique métier) : contrôles préalables
peu coûteux (CRC du ZIP, référence lisible, dépôt modèle présent) pour échouer AVANT des heures
de GPU, validation du rapport écrit pendant ce run, et traduction des erreurs en messages clairs.

Exécution en ligne de commande (même verrou que l'application) :
    python pipeline.py --zip /mnt/kyc_app/input/kyc_documents_test.zip
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from config import AppConfig, load_config
from exceptions import (AppConfigurationError, CudaUnavailableError, InvalidReferenceFileError, InvalidZipError,
                        KycAppError, MissingReferenceFileError, ModelLoadError, PipelineCancelled,
                        PipelineExecutionError, ReportGenerationError)
from file_manager import check_writable, full_integrity_check, inspect_zip
from logging_config import configure_logging, get_logger
from pipeline_context import PipelineContext, PipelineResult
from progress import CancelToken, ProgressCallback, ProgressEvent, console_progress, utc_now
import reporting


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]


def _emit(ctx: PipelineContext, stage: str, message: str, current=None, total=None, **counters) -> None:
    ctx.logger.info("run=%s stage=%s %s", ctx.run_id, stage, message)
    if ctx.progress is None:
        return
    try:
        ctx.progress(ProgressEvent(stage=stage, message=message, current=current, total=total, counters=counters))
    except Exception:  # noqa: BLE001
        ctx.logger.exception("Callback de progression en échec (ignoré)")


# --- 1. contrôles préalables (sans GPU) -------------------------------------------------------
def _preflight(ctx: PipelineContext) -> None:
    cfg = ctx.cfg
    _emit(ctx, "VALIDATION", "Contrôle de l'archive (structure et sécurité des chemins)", 0, 5)
    if not ctx.zip_path.is_file():
        raise InvalidZipError(f"Archive absente : {ctx.zip_path}")
    inspection = inspect_zip(ctx.zip_path, ctx.zip_path.name)
    if not inspection.valid:
        raise InvalidZipError("; ".join(inspection.errors), "L'archive ZIP n'est pas valide : " + " ".join(inspection.errors))
    _emit(ctx, "VALIDATION", "Contrôle d'intégrité CRC de l'archive", 1, 5)
    full_integrity_check(ctx.zip_path)
    _emit(ctx, "VALIDATION", "Contrôle de l'environnement (référence, modèle, stockage)", 2, 5)
    if not cfg.reference_csv.is_file():
        raise MissingReferenceFileError(f"Référence absente : {cfg.reference_csv}")
    if not (cfg.model_path / "config.json").is_file():
        raise ModelLoadError(f"config.json absent : {cfg.model_path}",
                             f"Le dépôt local du modèle Qwen3.8 est introuvable : {cfg.model_path}")
    for directory in (cfg.output_root, cfg.extract_dir):
        ok, detail = check_writable(directory)
        if not ok:
            raise AppConfigurationError(detail, f"Écriture impossible dans {directory}. Vérifier les droits sous /mnt.")


def _load_engine(ctx: PipelineContext):
    _emit(ctx, "VALIDATION", "Initialisation du moteur d'analyse (première exécution : import PyTorch/Transformers)", 3, 5)
    import engine

    try:
        return engine.load_engine(ctx.cfg)
    except engine.EngineIntegrityError as exc:
        raise AppConfigurationError(str(exc), "Le moteur d'analyse ne correspond pas au notebook de référence. "
                                              "Exécuter scripts/build_engine.sh puis redémarrer l'application.") from exc


def _validate_reference(ctx: PipelineContext, core) -> None:
    """Lecture de validation par la fonction d'origine du notebook (sans émettre l'étape 7)."""
    from engine import instrumentation

    _emit(ctx, "VALIDATION", "Validation du schéma de la base de recertification", 4, 5)
    try:
        reference = instrumentation.original("load_reference_csv")(ctx.cfg.reference_csv)
    except FileNotFoundError as exc:
        raise MissingReferenceFileError(str(exc)) from exc
    except (ValueError, UnicodeDecodeError, OSError) as exc:
        raise InvalidReferenceFileError(str(exc)) from exc
    _emit(ctx, "VALIDATION", f"Archive et référence valides ({reference['row_count']} lignes de référence)", 5, 5)


# --- 2. classification des erreurs ------------------------------------------------------------
def _exception_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def classify_exception(exc: BaseException, stage: str | None, in_final_audit: bool, cfg: AppConfig) -> KycAppError:
    if isinstance(exc, KycAppError):
        return exc
    chain = list(_exception_chain(exc))
    names = {type(e).__name__ for e in chain}
    text = " | ".join(f"{type(e).__name__}: {e}" for e in chain)
    lowered = text.lower()
    if any(isinstance(e, zipfile.BadZipFile) for e in chain) or any(
            marker in text for marker in ("Chemin ZIP interdit", "Archive introuvable", "Aucun dossier client",
                                          "PDF cible sans dossier client", "ZIP chiffré", "Ratio de compression",
                                          "PDF trop volumineux", "Lien ou entrée spéciale ZIP")):
        return InvalidZipError(text)
    if any(isinstance(e, FileNotFoundError) and str(cfg.reference_csv) in str(e) for e in chain):
        return MissingReferenceFileError(text)
    if "FatalCudaError" in names:
        return CudaUnavailableError(text, "Le contexte GPU est devenu inutilisable. Redémarrer l'application ; "
                                          "les résultats déjà sauvegardés seront réutilisés.")
    if "CUDA indisponible" in text or "cuda unavailable" in lowered:
        return CudaUnavailableError(text)
    if "OutOfMemoryError" in names or "cuda out of memory" in lowered:
        return ModelLoadError(text, "Mémoire GPU insuffisante. Vérifier que le tier matériel H100 est sélectionné "
                                    "et qu'aucun autre processus n'occupe le GPU.")
    if "BenchmarkGateError" in names:
        return PipelineExecutionError(text, "Le contrôle préalable GPU (benchmark) a échoué : configuration modèle "
                                            "ou GPU inadaptée. Aucun traitement de masse n'a été lancé.")
    if "PersistenceError" in names:
        return PipelineExecutionError(text, "Écriture impossible dans le stockage de sortie (/mnt). "
                                            "Les résultats déjà sauvegardés pourront être réutilisés.")
    if any(marker in text for marker in ("Qwen3.8 local absent", "Compatibilité Qwen3.8", "Quantification",
                                         "configuration vision", "Composants visuels", "BF16", "FP8",
                                         "Embeddings d'entrée", "ALERTE CPU/DISQUE/META")):
        return ModelLoadError(text)
    if stage == "REPORT" and not in_final_audit:
        return ReportGenerationError(text)
    if in_final_audit:
        return PipelineExecutionError(text, "Le contrôle final de cohérence de la population a échoué : "
                                            "le rapport n'est pas validé. Consulter le journal.")
    return PipelineExecutionError(text)


# --- 3. orchestration ---------------------------------------------------------------------------
def run_kyc_pipeline(zip_path: Path, output_dir: Path | None = None, progress_callback: ProgressCallback | None = None,
                     *, run_id: str | None = None, cancel_token: CancelToken | None = None,
                     cfg: AppConfig | None = None) -> PipelineResult:
    """Traite une archive de dossiers KYC et renvoie un résultat (ne lève pas d'exception métier)."""
    cfg = cfg or load_config()
    logger = get_logger("pipeline")
    started_epoch = time.time()
    ctx = PipelineContext(cfg=cfg, run_id=run_id or new_run_id(), zip_path=Path(zip_path), started_at=utc_now(),
                          started_epoch=started_epoch, progress=progress_callback, cancel_token=cancel_token,
                          logger=logger)
    result = PipelineResult(run_id=ctx.run_id, status="FAILED", success=False, user_message="",
                            input_zip=str(ctx.zip_path), run_mode=cfg.run_mode, started_at=ctx.started_at)
    if output_dir is not None and Path(output_dir) != cfg.output_root:
        result.user_message = "Le répertoire de sortie demandé diffère de la configuration (KYC_OUTPUT_ROOT)."
        result.error_kind = "CONFIGURATION"
        return _finalize(ctx, result)
    logger.info("run=%s DÉBUT traitement | zip=%s | mode=%s", ctx.run_id, ctx.zip_path.name, cfg.run_mode)
    core = None
    from engine import instrumentation

    try:
        _preflight(ctx)
        core = _load_engine(ctx)
        import engine

        _validate_reference(ctx, core)
        ctx.engine_session_id = engine.reset_run_state(core, ctx.zip_path)
        result.engine_session_id = ctx.engine_session_id
        instrumentation.activate(progress_callback or (lambda event: None), cancel_token)
        # Cellules d'exécution du notebook, dans l'ordre du notebook.
        core._nb_stage_inventory()
        core._nb_stage_pass_a()
        core._nb_stage_pass_b()
        core._nb_stage_matching_and_reports()
        run = instrumentation.current()
        result.model_loaded_this_run = bool(run and run.model_loaded_during_run)
        _emit(ctx, "REPORT", "Vérification du rapport Excel produit par ce traitement", 2, 2)
        report = reporting.validate_report(cfg.report_path, started_epoch)
        summary = reporting.build_summary(core, cfg, started_epoch)
        for key, value in summary.items():
            if hasattr(result, key):
                setattr(result, key, value)
        result.report_path, result.report_sha256 = report.path, report.sha256
        result.report_size_bytes, result.report_mtime_ns = report.size_bytes, report.mtime_ns
        anomalous = bool(result.anomalies) or result.processing_error_count > 0
        result.status = "SUCCESS_WITH_ANOMALIES" if anomalous else "SUCCESS"
        result.success = True
        result.user_message = "Traitement terminé avec anomalies" if anomalous else "Traitement terminé"
        logger.info("run=%s RAPPORT validé | %s | %d octets | sha256=%s", ctx.run_id, report.path, report.size_bytes, report.sha256)
    except PipelineCancelled as exc:
        result.status, result.error_kind = "CANCELLED", "CANCELLED"
        result.user_message = exc.user_message
        logger.warning("run=%s ARRÊT demandé par l'utilisateur, appliqué entre deux documents", ctx.run_id)
    except BaseException as exc:  # noqa: BLE001 — tout est journalisé puis traduit
        run = instrumentation.current()
        error = classify_exception(exc, run.stage if run else None, bool(run and run.in_final_audit), cfg)
        result.status = "REPORT_FAILED" if isinstance(error, ReportGenerationError) else "FAILED"
        result.error_kind = error.kind
        result.user_message = error.user_message
        result.errors.append(error.user_message)
        logger.error("run=%s ÉCHEC [%s] %s\n%s", ctx.run_id, error.kind, error.detail or error,
                     "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        if core is not None:
            try:  # Les compteurs produits avant l'échec restent informatifs (pas de statut global).
                partial = reporting.build_summary(core, cfg, started_epoch)
                result.technical = partial["technical"]
                result.qwen_calls = partial["qwen_calls"]
            except Exception:  # noqa: BLE001
                logger.exception("run=%s synthèse partielle impossible", ctx.run_id)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not isinstance(exc, PipelineCancelled):
            _finalize(ctx, result)
            raise
    finally:
        instrumentation.deactivate()
        if core is not None:
            try:
                core.cleanup_document()  # libération mémoire du notebook (cache de rendu, CUDA cache)
            except Exception:  # noqa: BLE001
                logger.exception("run=%s nettoyage mémoire en échec", ctx.run_id)
    return _finalize(ctx, result)


def _finalize(ctx: PipelineContext, result: PipelineResult) -> PipelineResult:
    result.finished_at = utc_now()
    result.elapsed_seconds = round(time.time() - ctx.started_epoch, 1)
    ctx.logger.info("run=%s FIN statut=%s durée=%.1fs clients=%d documents=%d qwen_calls=%d", ctx.run_id, result.status,
                    result.elapsed_seconds, result.customers_processed, result.documents_processed, result.qwen_calls)
    return result


def main(argv: list[str] | None = None) -> int:
    """Exécution hors interface (workspace Domino, tests de régression), sous le même verrou."""
    from file_manager import write_run_metadata
    from run_lock import RunLock
    from exceptions import ConcurrentRunError

    parser = argparse.ArgumentParser(description="Pipeline KYC (moteur du notebook) en ligne de commande")
    parser.add_argument("--zip", type=Path, required=True, help="archive ZIP déjà placée sur /mnt")
    args = parser.parse_args(argv)
    cfg = load_config()
    configure_logging(cfg)
    run_id = new_run_id()
    lock = RunLock(cfg.lock_path, cfg.lock_stale_after_s)
    try:
        lock.acquire(run_id)
    except ConcurrentRunError as exc:
        print(exc.user_message, file=sys.stderr)
        return 3
    try:
        result = run_kyc_pipeline(args.zip.resolve(), progress_callback=console_progress, run_id=run_id, cfg=cfg)
        write_run_metadata(cfg, run_id, {"origin": "CLI", **result.to_dict()})
    finally:
        lock.release()
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0 if result.success else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
