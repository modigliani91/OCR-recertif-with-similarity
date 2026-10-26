"""Upload : archive valide, invalide, vide, chemins dangereux, structure ; écriture atomique ; droits."""
import io
import zipfile
from pathlib import Path

import pytest

from exceptions import InvalidZipError
from file_manager import check_writable, inspect_zip, persist_upload, readiness
from synthetic_data import build_dataset, make_zip

PDF = b"%PDF-1.4 test"


def test_valid_archive(tmp_path):
    data = build_dataset(tmp_path)
    report = inspect_zip(data["zip"], data["zip"].name)
    assert report.valid, report.errors
    assert report.customer_count == 3 and report.target_pdf_count == 15
    assert report.customer_root == "kyc_documents_test"


@pytest.mark.parametrize("entries,needle", [
    ({}, "vide"),
    ({"kyc_documents_test/C1/": b""}, "vide"),
    ({"../evil/JUSTIFICATIF IDENTITE.PDF": PDF}, "chemin interdit"),
    ({"/abs/C1/FATCA.PDF": PDF}, "chemin interdit"),
    ({"C:/x/C1/FATCA.PDF": PDF}, "chemin interdit"),
    ({"FATCA.PDF": PDF}, "hors dossier client"),
    ({"kyc_documents_test/C1/notes.txt": b"x"}, "Aucun document KYC"),
])
def test_invalid_archives(tmp_path, entries, needle):
    archive = make_zip(tmp_path / "kyc_documents_test.zip", entries)
    report = inspect_zip(archive, archive.name)
    assert not report.valid
    assert any(needle in error for error in report.errors), report.errors


def test_not_a_zip(tmp_path):
    path = tmp_path / "kyc_documents_test.zip"
    path.write_bytes(b"not a zip at all")
    report = inspect_zip(path, path.name)
    assert not report.valid and "pas une archive ZIP" in report.errors[0]


@pytest.mark.parametrize("name", ["", "a/b.zip", "..\\x.zip", "document.pdf", "x" * 300 + ".zip"])
def test_rejected_names(tmp_path, name):
    archive = make_zip(tmp_path / "ok.zip", {"kyc/C1/FATCA.PDF": PDF})
    assert not inspect_zip(archive, name).valid


def test_upload_fileobj_inspection(tmp_path):
    data = build_dataset(tmp_path)
    buffer = io.BytesIO(data["zip"].read_bytes())
    assert inspect_zip(buffer, data["zip"].name, len(buffer.getvalue())).valid
    assert buffer.tell() == 0  # le flux est rembobiné pour l'écriture


def test_persist_upload_atomic(tmp_path):
    data = build_dataset(tmp_path / "data")
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "old_batch.zip").write_bytes(b"old")
    with data["zip"].open("rb") as handle:
        saved = persist_upload(handle, data["zip"].name, input_dir)
    assert saved == input_dir / "kyc_documents_test.zip"
    assert saved.read_bytes() == data["zip"].read_bytes()
    assert not (input_dir / "old_batch.zip").exists()
    assert not list(input_dir.glob(".upload-*"))


def test_persist_upload_rejects_invalid(tmp_path):
    input_dir = tmp_path / "input"
    with pytest.raises(InvalidZipError):
        persist_upload(io.BytesIO(b"garbage"), "kyc_documents_test.zip", input_dir)
    assert not (input_dir / "kyc_documents_test.zip").exists()
    assert not list(input_dir.glob(".upload-*"))


def test_writable_checks(tmp_path):
    ok, _ = check_writable(tmp_path / "out")
    assert ok
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ok, detail = check_writable(blocker / "sub")
    assert not ok and "sub" in detail


def test_readiness_reports_missing_reference_and_model():
    from config import load_config

    checks = {c.name: c for c in readiness(load_config())}
    assert not checks["Fichier de référence présent"].ok
    assert not checks["Dépôt modèle Qwen3.8 local présent"].ok
    assert checks["Sorties pipeline accessibles en écriture"].ok
    assert checks["Moteur généré conforme au notebook"].ok
