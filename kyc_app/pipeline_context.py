"""Contexte d'exécution et résultat d'un traitement (structures simples, sans logique métier)."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import AppConfig
from progress import CancelToken, ProgressCallback

RESULT_STATUSES = ("SUCCESS", "SUCCESS_WITH_ANOMALIES", "FAILED", "REPORT_FAILED", "CANCELLED")


@dataclass
class PipelineContext:
    cfg: AppConfig
    run_id: str
    zip_path: Path
    started_at: str
    started_epoch: float
    progress: ProgressCallback | None
    cancel_token: CancelToken | None
    logger: logging.Logger
    engine_session_id: str | None = None


@dataclass
class PipelineResult:
    run_id: str
    status: str
    success: bool
    user_message: str
    report_path: Path | None = None
    report_sha256: str | None = None
    report_size_bytes: int | None = None
    report_mtime_ns: int | None = None
    customers_discovered: int = 0
    customers_selected: int = 0
    customers_processed: int = 0
    documents_processed: int = 0
    documents_missing: int = 0
    duplicates_skipped: int = 0
    raw_pages_processed: int = 0
    conform_count: int = 0
    non_conform_count: int = 0
    recheck_count: int = 0
    reference_not_found_count: int = 0
    processing_error_count: int = 0
    manual_review_count: int = 0
    qwen_calls: int = 0
    model_loaded_this_run: bool = False
    anomalies: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    error_kind: str | None = None
    engine_session_id: str | None = None
    input_zip: str | None = None
    run_mode: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float = 0.0
    technical: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["report_path"] = str(self.report_path) if self.report_path else None
        return data
