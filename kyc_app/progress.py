"""Événements de progression : pipeline → callback → interface.

Le moteur et le pipeline ne connaissent pas Streamlit. Ils émettent des ``ProgressEvent``
vers un simple callable. ``ProgressTracker`` agrège ces événements dans un instantané
thread-safe lu par l'interface, et l'écrit régulièrement sur disque (état durable :
``<OUTPUT_ROOT>/app_runs/<run_id>/status.json``).
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from exceptions import PipelineCancelled

# (clé, libellé affiché, poids dans la barre globale). Les poids reflètent le coût observé :
# l'OCR RAW et l'extraction structurée dominent le temps GPU.
STAGES: list[tuple[str, str, float]] = [
    ("VALIDATION", "Validation de l'archive", 0.02),
    ("EXTRACT", "Extraction ZIP", 0.03),
    ("CUSTOMERS", "Inventaire clients", 0.01),
    ("DOCUMENTS", "Inventaire documents", 0.02),
    ("RAW_OCR", "RAW OCR", 0.55),
    ("STRUCTURE", "Extraction structurée", 0.27),
    ("MATCHING", "Matching base clients", 0.01),
    ("CONFORMITY", "Contrôles de conformité", 0.06),
    ("REPORT", "Génération du rapport Excel", 0.03),
]
STAGE_KEYS = [key for key, _, _ in STAGES]
STAGE_LABELS = {key: label for key, label, _ in STAGES}
TERMINAL_STATES = {"SUCCESS", "SUCCESS_WITH_ANOMALIES", "FAILED", "REPORT_FAILED", "CANCELLED"}


@dataclass
class ProgressEvent:
    stage: str
    message: str
    current: int | None = None
    total: int | None = None
    customer_id: str | None = None  # conservé pour les journaux techniques, jamais affiché par défaut
    counters: dict = field(default_factory=dict)


ProgressCallback = Callable[[ProgressEvent], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CancelToken:
    """Demande d'arrêt coopérative, consultée uniquement aux frontières sûres (document/client)."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def raise_if_requested(self) -> None:
        if self._event.is_set():
            raise PipelineCancelled("Arrêt demandé par l'utilisateur")


class ProgressTracker:
    """Agrège les événements en un instantané unique, sûr pour plusieurs sessions Streamlit."""

    def __init__(self, run_id: str, status_path: Path | None = None, flush_interval_s: float = 2.0,
                 on_event: Callable[[ProgressEvent], None] | None = None):
        self._lock = threading.Lock()
        self._status_path = status_path
        self._flush_interval_s = flush_interval_s
        self._last_flush = 0.0
        self._on_event = on_event
        self._started_monotonic = time.monotonic()
        self._snapshot = {
            "run_id": run_id, "state": "RUNNING", "started_at": utc_now(), "updated_at": utc_now(),
            "finished_at": None, "stage": None, "stage_index": 0, "stage_label": None, "stage_message": None,
            "stage_current": None, "stage_total": None, "completed_stages": [], "fraction": 0.0,
            "counters": {}, "cancel_requested": False, "result": None, "error": None, "elapsed_s": 0.0,
        }

    def __call__(self, event: ProgressEvent) -> None:
        self.on_event(event)

    def on_event(self, event: ProgressEvent) -> None:
        if event.stage not in STAGE_LABELS:
            return
        with self._lock:
            snap = self._snapshot
            new_index = STAGE_KEYS.index(event.stage) + 1
            if new_index > snap["stage_index"]:
                snap["completed_stages"] = STAGE_KEYS[: new_index - 1]
                snap["stage_index"] = new_index
                snap["stage"] = event.stage
                snap["stage_label"] = STAGE_LABELS[event.stage]
                snap["stage_current"] = snap["stage_total"] = None
                stage_changed = True
            else:
                stage_changed = False
            if event.stage == snap["stage"]:
                snap["stage_message"] = event.message
                if event.total is not None:
                    snap["stage_current"], snap["stage_total"] = event.current, event.total
            snap["counters"].update(event.counters)
            snap["fraction"] = self._fraction(snap)
            snap["updated_at"] = utc_now()
            snap["elapsed_s"] = round(time.monotonic() - self._started_monotonic, 1)
            self._maybe_flush(force=stage_changed)
        if self._on_event is not None:
            self._on_event(event)

    @staticmethod
    def _fraction(snap: dict) -> float:
        done = sum(weight for key, _, weight in STAGES if key in snap["completed_stages"])
        if snap["stage"]:
            weight = dict((k, w) for k, _, w in STAGES)[snap["stage"]]
            current, total = snap["stage_current"], snap["stage_total"]
            if current is not None and total:
                done += weight * min(1.0, max(0.0, current / total))
        return round(min(0.99, done), 4)  # 100 % uniquement après validation du rapport

    def mark_cancel_requested(self) -> None:
        with self._lock:
            self._snapshot["cancel_requested"] = True
            self._maybe_flush(force=True)

    def finish(self, state: str, result: dict | None = None, error: dict | None = None) -> None:
        with self._lock:
            snap = self._snapshot
            snap["state"] = state
            snap["finished_at"] = snap["updated_at"] = utc_now()
            snap["elapsed_s"] = round(time.monotonic() - self._started_monotonic, 1)
            snap["result"] = result
            snap["error"] = error
            if state in {"SUCCESS", "SUCCESS_WITH_ANOMALIES"}:
                snap["completed_stages"] = list(STAGE_KEYS)
                snap["fraction"] = 1.0
            self._maybe_flush(force=True)

    def snapshot(self) -> dict:
        with self._lock:
            snap = copy.deepcopy(self._snapshot)
        if snap["state"] == "RUNNING":
            snap["elapsed_s"] = round(time.monotonic() - self._started_monotonic, 1)
        return snap

    def _maybe_flush(self, force: bool = False) -> None:
        if self._status_path is None:
            return
        now = time.monotonic()
        if not force and now - self._last_flush < self._flush_interval_s:
            return
        self._last_flush = now
        try:
            atomic_write_json(self._status_path, self._snapshot)
        except OSError:
            # L'état en mémoire reste exact ; une indisponibilité disque est signalée par le pipeline.
            pass


def console_progress(event: ProgressEvent) -> None:
    """Callback minimal pour l'exécution en ligne de commande (python -m pipeline)."""
    position = f" [{event.current}/{event.total}]" if event.total else ""
    print(f"[APP PROGRESS] {STAGE_LABELS.get(event.stage, event.stage)}{position} — {event.message}", flush=True)


def event_dict(event: ProgressEvent) -> dict:
    return asdict(event)
