"""Verrou fichier : un seul pipeline GPU lourd à la fois, robuste aux redémarrages.

Le fichier ``<OUTPUT_ROOT>/.kyc_processing.lock`` contient pid, hôte, run_id, date de début
et dernier battement de cœur. Il est créé de façon exclusive (O_CREAT | O_EXCL).

Détection d'un verrou orphelin (prudente) :
  * même hôte et processus propriétaire mort                → orphelin ;
  * même hôte, même pid que le processus courant, mais aucun
    traitement actif dans ce processus                        → orphelin (reste d'un échec) ;
  * battement de cœur plus ancien que ``stale_after_s``       → orphelin (redémarrage Domino :
    nouveau conteneur, autre hôte) ;
  * fichier illisible : orphelin seulement si sa date de modification est ancienne.
Un verrou orphelin est renommé ``.kyc_processing.lock.stale-<horodatage>`` (trace conservée).

Récupération manuelle :
    python -m run_lock --status
    python -m run_lock --break          # uniquement si le verrou est orphelin
    python -m run_lock --break --force  # en dernier recours, après avoir vérifié qu'aucun traitement ne tourne
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from exceptions import ConcurrentRunError
from progress import utc_now


@dataclass
class LockStatus:
    exists: bool
    stale: bool
    reason: str
    info: dict


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunLock:
    def __init__(self, path: Path, stale_after_s: int = 900):
        self.path = Path(path)
        self.stale_after_s = stale_after_s
        self.run_id: str | None = None

    # -- lecture / diagnostic -------------------------------------------------
    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"corrupt": True}

    def status(self, local_job_active: bool = False) -> LockStatus:
        info = self.read()
        if info is None:
            return LockStatus(False, False, "NO_LOCK", {})
        if info.get("corrupt"):
            try:
                age = time.time() - self.path.stat().st_mtime
            except FileNotFoundError:
                return LockStatus(False, False, "NO_LOCK", {})
            stale = age > self.stale_after_s
            return LockStatus(True, stale, "CORRUPT_OLD" if stale else "CORRUPT_RECENT", info)
        same_host = info.get("host") == socket.gethostname()
        pid = int(info.get("pid", -1))
        if same_host and pid == os.getpid() and not local_job_active:
            return LockStatus(True, True, "OWN_PROCESS_NO_ACTIVE_JOB", info)
        if same_host and pid != os.getpid() and not _pid_alive(pid):
            return LockStatus(True, True, "OWNER_PROCESS_DEAD", info)
        age = time.time() - float(info.get("heartbeat_epoch", 0))
        if age > self.stale_after_s:
            return LockStatus(True, True, f"HEARTBEAT_OLDER_THAN_{self.stale_after_s}S", info)
        return LockStatus(True, False, "ACTIVE", info)

    # -- cycle de vie -------------------------------------------------------------
    def acquire(self, run_id: str, local_job_active: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            payload = {"run_id": run_id, "pid": os.getpid(), "host": socket.gethostname(),
                       "started_at": utc_now(), "heartbeat_at": utc_now(), "heartbeat_epoch": time.time()}
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                state = self.status(local_job_active)
                if state.exists and not state.stale:
                    raise ConcurrentRunError(f"Verrou actif : {state.info}") from None
                if attempt == 0:
                    self._quarantine(state.reason)
                    continue
                raise ConcurrentRunError("Verrou concurrent recréé pendant l'acquisition") from None
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            self.run_id = run_id
            return
        raise ConcurrentRunError("Verrou indisponible")

    def heartbeat(self) -> None:
        info = self.read()
        if not info or info.get("run_id") != self.run_id:
            return
        info["heartbeat_at"], info["heartbeat_epoch"] = utc_now(), time.time()
        temporary = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(info), encoding="utf-8")
        os.replace(temporary, self.path)

    def release(self) -> None:
        info = self.read()
        if info and info.get("run_id") == self.run_id:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.run_id = None

    def _quarantine(self, reason: str) -> None:
        target = self.path.with_name(f"{self.path.name}.stale-{time.strftime('%Y%m%dT%H%M%S')}-{reason}")
        try:
            os.replace(self.path, target)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    from config import load_config

    parser = argparse.ArgumentParser(description="Diagnostic du verrou de traitement KYC")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--break", dest="break_lock", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config()
    lock = RunLock(cfg.lock_path, cfg.lock_stale_after_s)
    # Ce CLI tourne dans un autre processus que l'application : local_job_active=True évite de
    # qualifier d'orphelin un verrou au seul motif que le pid correspondrait par coïncidence.
    state = lock.status(local_job_active=True)
    print(json.dumps({"path": str(lock.path), "exists": state.exists, "stale": state.stale,
                      "reason": state.reason, "info": state.info}, ensure_ascii=False, indent=2))
    if args.break_lock:
        if not state.exists:
            print("Aucun verrou.")
            return 0
        if not state.stale and not args.force:
            print("Verrou ACTIF : refus. Vérifier qu'aucun traitement ne tourne puis utiliser --force.", file=sys.stderr)
            return 1
        lock._quarantine("MANUAL_FORCE" if args.force and not state.stale else "MANUAL")
        print("Verrou mis en quarantaine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
