"""Test d'intégration de bout en bout SANS GPU : JobManager → pipeline → moteur du notebook.

Exécuté dans un processus dédié (la configuration est fixée à l'import), avec des dossiers
temporaires isolés : n'écrit jamais dans /mnt/output_qwen38.

    python tests/dryrun_harness.py --workdir /tmp/kyc_dryrun --scenario full
    python tests/dryrun_harness.py --workdir /tmp/kyc_dryrun_cancel --scenario cancel

Scénario « full » : 1er traitement complet (rapport validé, téléchargeable), 2e traitement de
la même archive (toutes les pages RAW réutilisées depuis les checkpoints), verrou libéré.
Scénario « cancel » : arrêt demandé pendant le RAW OCR → CANCELLED, verrou libéré, puis
reprise complète réutilisant les pages déjà transcrites.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def wait_terminal(manager, timeout_s: float = 600.0, on_tick=None) -> dict:
    from progress import TERMINAL_STATES

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snapshot = manager.snapshot()
        if on_tick:
            on_tick(snapshot)
        if snapshot and snapshot["state"] in TERMINAL_STATES and not manager.is_active():
            return snapshot
        time.sleep(0.2)
    raise TimeoutError("Traitement non terminé dans le délai")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--scenario", choices=("full", "cancel"), default="full")
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    from synthetic_data import build_dataset

    data = build_dataset(workdir / "data")
    os.environ.update({
        "KYC_OUTPUT_ROOT": str(workdir / "output"), "KYC_APP_ROOT": str(workdir / "app"),
        "KYC_REFERENCE_CSV": str(data["reference"]), "KYC_MODEL_PATH": str(data["model"]),
        "KYC_RUN_MODE": "FULL_PIPELINE", "KYC_LOCK_HEARTBEAT_S": "1", "KYC_LOCK_STALE_AFTER_S": "60",
    })
    from config import load_config
    from fake_qwen import CALLS, install_fake_qwen
    from job_manager import JobManager
    from logging_config import configure_logging
    from reporting import read_report_for_download
    import engine

    cfg = load_config()
    configure_logging(cfg)
    core = engine.load_engine(cfg)
    install_fake_qwen(core, page_delay_s=0.25 if args.scenario == "cancel" else 0.0)
    manager = JobManager(cfg)
    summary = {"scenario": args.scenario}
    stages_seen: list[str] = []

    def track(snapshot):
        if snapshot and snapshot.get("stage") and snapshot["stage"] not in stages_seen:
            stages_seen.append(snapshot["stage"])

    def start():
        with data["zip"].open("rb") as handle:
            return manager.start(handle, data["zip"].name)

    if args.scenario == "cancel":
        start()
        cancelled = False

        def cancel_during_raw(snapshot):
            nonlocal cancelled
            track(snapshot)
            if not cancelled and snapshot and (snapshot.get("counters") or {}).get("pages_ocr_done", 0) >= 3:
                cancelled = manager.request_cancel()

        final = wait_terminal(manager, on_tick=cancel_during_raw)
        assert final["state"] == "CANCELLED", final
        assert not cfg.lock_path.exists(), "verrou non libéré après arrêt"
        done_before = final["counters"].get("pages_ocr_done", 0)
        calls_before = CALLS["infer_image"]
        start()
        resumed = wait_terminal(manager, on_tick=track)
        assert resumed["state"].startswith("SUCCESS"), resumed
        reused = resumed["counters"].get("pages_ocr_reused", 0)
        assert reused >= done_before, (reused, done_before)
        summary.update(pages_before_cancel=done_before, pages_reused_after_restart=reused,
                       ocr_calls_first_run=calls_before, ocr_calls_after_restart=CALLS["infer_image"] - calls_before)
    else:
        start()
        try:
            start()
            raise AssertionError("un second traitement simultané a été accepté")
        except Exception as exc:  # ConcurrentRunError attendu
            assert type(exc).__name__ == "ConcurrentRunError", exc
        first = wait_terminal(manager, on_tick=track)
        assert first["state"] in {"SUCCESS", "SUCCESS_WITH_ANOMALIES"}, first
        assert first["fraction"] == 1.0
        metadata = manager.last_run()
        report = read_report_for_download(cfg.report_path, metadata["report_sha256"], metadata["report_mtime_ns"])
        assert report and report[:2] == b"PK", "rapport non téléchargeable"
        assert not cfg.lock_path.exists(), "verrou non libéré"
        on_disk = json.loads((cfg.runs_dir / "last_run.json").read_text(encoding="utf-8"))
        assert on_disk["run_id"] == metadata["run_id"] and on_disk["report_sha256"] == metadata["report_sha256"]
        calls_first = dict(CALLS)
        start()
        second = wait_terminal(manager)
        meta2 = manager.last_run()
        pages_total = second["counters"].get("pages_ocr_total")
        assert second["counters"].get("pages_ocr_reused") == pages_total, second["counters"]
        assert CALLS["infer_image"] == calls_first.get("infer_image"), "RAW OCR relancé malgré les checkpoints"
        assert meta2["report_sha256"] and meta2["run_id"] != metadata["run_id"]
        keys = ("customers_discovered", "customers_processed", "documents_processed", "conform_count",
                "non_conform_count", "recheck_count", "reference_not_found_count", "processing_error_count",
                "manual_review_count")
        assert all(metadata[k] == meta2[k] for k in keys), "résultats différents entre run initial et reprise"
        summary.update(first_run={k: metadata[k] for k in keys + ("status", "qwen_calls", "anomalies")},
                       second_run_qwen_calls=meta2["qwen_calls"], pages_total=pages_total,
                       simulated_calls_first_run=calls_first, report_size_bytes=len(report))
    summary["stages_seen"] = stages_seen
    summary["report_path"] = str(cfg.report_path)
    print("DRYRUN_SUMMARY " + json.dumps(summary, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
