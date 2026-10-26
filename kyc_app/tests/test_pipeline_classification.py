"""Traduction des erreurs techniques en messages utilisateur (sans trace Python)."""
import zipfile
from pathlib import Path

from config import load_config
from exceptions import (CudaUnavailableError, InvalidZipError, MissingReferenceFileError, ModelLoadError,
                        PipelineExecutionError, ReportGenerationError)
from pipeline import classify_exception, run_kyc_pipeline


class FatalCudaError(RuntimeError):
    pass


class PersistenceError(RuntimeError):
    pass


def test_classification():
    cfg = load_config()
    assert isinstance(classify_exception(zipfile.BadZipFile("x"), "EXTRACT", False, cfg), InvalidZipError)
    assert isinstance(classify_exception(ValueError("Chemin ZIP interdit : '../a'"), "EXTRACT", False, cfg), InvalidZipError)
    assert isinstance(classify_exception(FileNotFoundError(str(cfg.reference_csv)), "MATCHING", False, cfg),
                      MissingReferenceFileError)
    assert isinstance(classify_exception(RuntimeError("CUDA indisponible : …"), "RAW_OCR", False, cfg), CudaUnavailableError)
    assert isinstance(classify_exception(FatalCudaError("x"), "RAW_OCR", False, cfg), CudaUnavailableError)
    assert isinstance(classify_exception(RuntimeError("Qwen3.8 local absent : x"), "RAW_OCR", False, cfg), ModelLoadError)
    wrapped = RuntimeError("Compatibilité Qwen3.8 non établie")
    assert isinstance(classify_exception(wrapped, "RAW_OCR", False, cfg), ModelLoadError)
    assert isinstance(classify_exception(OSError("disk"), "REPORT", False, cfg), ReportGenerationError)
    audit = classify_exception(AssertionError("Missing customer result JSON"), "REPORT", True, cfg)
    assert isinstance(audit, PipelineExecutionError) and "contrôle final" in audit.user_message
    persistence = classify_exception(PersistenceError("x"), "STRUCTURE", False, cfg)
    assert "/mnt" in persistence.user_message
    generic = classify_exception(KeyError("x"), "STRUCTURE", False, cfg)
    assert generic.user_message.startswith("Le traitement a été interrompu")


def test_pipeline_rejects_missing_zip_without_engine(tmp_path):
    result = run_kyc_pipeline(Path(tmp_path / "absent.zip"))
    assert not result.success and result.error_kind == "INVALID_ZIP"
    assert result.user_message == "L'archive ZIP n'est pas valide."
