"""Rapport présent/absent/ancien/incomplet ; téléchargement vérifié ; progression et arrêt."""
import os
import time

import pytest
from openpyxl import Workbook

from exceptions import PipelineCancelled, ReportGenerationError
from progress import CancelToken, ProgressEvent, ProgressTracker
from reporting import REQUIRED_SHEETS, read_report_for_download, validate_report


def make_workbook(path, sheets=REQUIRED_SHEETS):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name in sheets:
        workbook.create_sheet(name).append(["A"])
    workbook.save(path)


def test_report_missing(tmp_path):
    with pytest.raises(ReportGenerationError):
        validate_report(tmp_path / "missing.xlsx", time.time())


def test_report_empty(tmp_path):
    path = tmp_path / "r.xlsx"
    path.write_bytes(b"")
    with pytest.raises(ReportGenerationError):
        validate_report(path, 0)


def test_report_from_previous_run_rejected(tmp_path):
    path = tmp_path / "r.xlsx"
    make_workbook(path)
    old = time.time() - 3600
    os.utime(path, (old, old))
    with pytest.raises(ReportGenerationError, match="antérieur"):
        validate_report(path, time.time())


def test_report_missing_sheets(tmp_path):
    path = tmp_path / "r.xlsx"
    make_workbook(path, ["SUMMARY"])
    with pytest.raises(ReportGenerationError, match="Feuilles manquantes"):
        validate_report(path, time.time() - 10)


def test_report_valid_and_download(tmp_path):
    path = tmp_path / "r.xlsx"
    started = time.time() - 1
    make_workbook(path)
    info = validate_report(path, started)
    assert info.size_bytes > 0 and len(info.sha256) == 64
    assert read_report_for_download(path, info.sha256, info.mtime_ns) == path.read_bytes()
    assert read_report_for_download(path, "0" * 64, info.mtime_ns) is None
    make_workbook(path)  # régénéré après le run : plus le même fichier
    os.utime(path, ns=(info.mtime_ns + 10**9, info.mtime_ns + 10**9))
    assert read_report_for_download(path, info.sha256, info.mtime_ns) is None


def test_tracker_progression(tmp_path):
    tracker = ProgressTracker("run", tmp_path / "status.json", flush_interval_s=0)
    tracker(ProgressEvent("VALIDATION", "v", 1, 1))
    tracker(ProgressEvent("RAW_OCR", "raw", 5, 10, counters={"customers_total": 3}))
    snap = tracker.snapshot()
    assert snap["stage_index"] == 5 and snap["completed_stages"][:4] == ["VALIDATION", "EXTRACT", "CUSTOMERS", "DOCUMENTS"]
    assert 0.08 < snap["fraction"] < 0.99 and snap["counters"]["customers_total"] == 3
    tracker(ProgressEvent("EXTRACT", "late event ignored for stage order", 1, 1))
    assert tracker.snapshot()["stage"] == "RAW_OCR"
    tracker.finish("SUCCESS", result={"ok": True})
    assert tracker.snapshot()["fraction"] == 1.0
    assert (tmp_path / "status.json").is_file()


def test_cancel_token():
    token = CancelToken()
    token.raise_if_requested()
    token.request()
    with pytest.raises(PipelineCancelled):
        token.raise_if_requested()
    assert issubclass(PipelineCancelled, KeyboardInterrupt)  # même chemin que l'interruption Jupyter du notebook
