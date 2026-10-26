"""Smoke test GPU (workspace Domino sur tier H100) : chargement réel de Qwen3.8 + une inférence RAW.

Utilise exclusivement le code du notebook (``ensure_model_loaded`` : FP8 déquantifié en BF16,
contrôle du placement GPU, template sans thinking ; ``run_attempt`` : rendu, prétraitement,
génération déterministe, validation de la transcription) sur une page synthétique fictive.

    python tools/gpu_smoke_test.py
    grep "GPU SMOKE" /mnt/output_qwen38/logs/kyc_app.log

Prend le verrou de traitement : ne pas lancer pendant un traitement de l'application.
Durée typique : chargement du modèle (plusieurs minutes) + une page.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(PROJECT_ROOT), str(PROJECT_ROOT / "tests")]

EXPECTED_WORDS = ("SPECIMEN", "SIGNATURE", "001234")


def main() -> int:
    from config import load_config
    from exceptions import ConcurrentRunError
    from logging_config import configure_logging
    from model_manager import ModelManager
    from run_lock import RunLock
    from synthetic_data import make_pdf
    import engine

    cfg = load_config()
    logger = configure_logging(cfg)
    lock = RunLock(cfg.lock_path, cfg.lock_stale_after_s)
    run_id = "gpu_smoke_" + uuid.uuid4().hex[:6]
    try:
        lock.acquire(run_id)
    except ConcurrentRunError:
        print("Un traitement est en cours : smoke test reporté.", file=sys.stderr)
        return 3
    try:
        core = engine.load_engine(cfg)
        workdir = Path(tempfile.mkdtemp(prefix="gpu_smoke_"))
        pdf_path = workdir / "smoke.pdf"
        make_pdf(pdf_path, [["SPECIMEN DE SIGNATURE", "Nom et Prénom : TEST SMOKE", "Numéro de compte : 001234"]])
        engine.reset_run_state(core, workdir / "unused.zip")
        manager = ModelManager(cfg)
        started = time.perf_counter()
        manager.ensure_loaded()
        load_s = time.perf_counter() - started
        info = manager.runtime_info()
        gpus = [g["name"] for g in info.get("gpus_torch", [])]
        devices = info.get("parameters_by_device") or {}
        logger.info("GPU SMOKE | H100 détecté=%s | GPUs=%s", any("H100" in g for g in gpus), gpus)
        logger.info("GPU SMOKE | modèle chargé en %.1fs | classe=%s | dtype=%s", load_s, info.get("model_class"), info.get("model_dtype"))
        logger.info("GPU SMOKE | device map validée (paramètres par device)=%s | thinking désactivé vérifié=%s",
                    devices, info.get("thinking_disabled_verified"))
        page = {"customer_id": "GPU_SMOKE", "logical_document_type": "SMOKE", "customer_relative_path": "smoke.pdf",
                "physical_filename": "smoke.pdf", "page_index": 0, "page_number": 1, "page_count": 1,
                "page_key": "gpu_smoke_" + uuid.uuid4().hex, "full_path": str(pdf_path)}
        import fitz

        with fitz.open(pdf_path) as pdf:
            attempt = core.run_attempt(page, pdf, "gpu_smoke")
        text = attempt.get("raw_ocr") or ""
        found = [w for w in EXPECTED_WORDS if w in text.upper()]
        summary = {
            "h100_detected": any("H100" in g for g in gpus), "gpus": gpus, "model_load_time_s": round(load_s, 1),
            "all_parameters_on_cuda": bool(devices) and all(k.startswith("cuda") for k in devices),
            "inference_status": attempt.get("status"), "stop_reason": attempt.get("stop_reason"),
            "tokens_in": attempt.get("tokens_in"), "tokens_out": attempt.get("tokens_out"),
            "generation_time_s": round(attempt.get("generation_time_s", 0.0), 2),
            "tokens_per_second": round(attempt.get("tokens_per_second", 0.0), 1),
            "gpu_memory_after_mb": attempt.get("gpu_memory_allocated_after_mb"),
            "expected_words_found": found, "qwen_calls": core.BUDGET.used,
        }
        logger.info("GPU SMOKE | inférence CUDA %s | %s", "OK" if attempt.get("status") == "SUCCESS" else "ÉCHEC", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        ok = (summary["all_parameters_on_cuda"] and attempt.get("status") == "SUCCESS"
              and len(found) == len(EXPECTED_WORDS))
        print("GPU SMOKE TEST :", "RÉUSSI" if ok else "ÉCHEC")
        return 0 if ok else 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
