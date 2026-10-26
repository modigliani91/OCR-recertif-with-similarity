"""Fichiers de l'application : contrôle préalable du ZIP, stockage atomique, métadonnées de run.

Le contrôle préalable (à l'upload) reprend les règles de sécurité et de structure du notebook
(``safe_member_path``, détection de la racine clients, noms de documents cibles lus dans le
manifeste du moteur) afin de refuser immédiatement une archive inutilisable. L'extraction
réelle reste exclusivement faite par ``safe_extract_zip`` du notebook pendant le traitement.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from config import AppConfig, load_engine_manifest
from exceptions import InvalidZipError
from progress import atomic_write_json, utc_now

_SAFE_UPLOAD_NAME = re.compile(r"^[^/\\\x00-\x1f]{1,200}\.zip$", re.IGNORECASE)
COPY_CHUNK = 8 * 1024 * 1024


@dataclass
class ZipInspection:
    filename: str
    size_bytes: int
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    member_count: int = 0
    customer_count: int = 0
    target_pdf_count: int = 0
    customer_root: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("octets", "Ko", "Mo", "Go"):
        if value < 1024 or unit == "Go":
            return f"{value:.0f} {unit}" if unit == "octets" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} octets"


# --- règles reprises du notebook (cellules 9–10), lues depuis le manifeste du moteur ---------
def _document_rules() -> dict:
    defaults = load_engine_manifest()["config_defaults"]
    return {"targets": tuple(defaults["TARGET_DOCUMENTS"]), "aliases": dict(defaults["DOCUMENT_ALIASES"]),
            "suffix": bool(defaults["RECOGNIZE_DUPLICATE_SUFFIX"]),
            "max_members": int(defaults.get("MAX_ZIP_MEMBERS") or 200000)}


def _normalized_filename(filename: str, recognize_suffix: bool) -> str:
    path = PurePosixPath(filename)
    stem = re.sub(r"\s+", " ", path.stem.strip()).upper()
    if recognize_suffix:
        stem = re.sub(r"\s*\(\d+\)$", "", stem).rstrip()
    return stem + path.suffix.upper()


def _is_target_document(filename: str, rules: dict) -> bool:
    lookup = {_normalized_filename(n, rules["suffix"]) for n in rules["targets"]}
    lookup |= {_normalized_filename(alias, rules["suffix"]) for alias in rules["aliases"]}
    return _normalized_filename(filename, rules["suffix"]) in lookup


def _unsafe_member_reason(info: zipfile.ZipInfo) -> str | None:
    raw = info.filename
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (not raw or "\x00" in raw or path.is_absolute() or PureWindowsPath(raw).drive
            or ".." in normalized.split("/") or any(":" in p for p in path.parts)):
        return f"chemin interdit : {raw!r}"
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
        return f"lien ou entrée spéciale interdite : {raw!r}"
    if not path.parts:
        return "entrée sans nom"
    return None


def inspect_zip(source: str | Path | BinaryIO, filename: str, size_bytes: int | None = None) -> ZipInspection:
    """Contrôle léger d'une archive (fichier sur disque ou objet fichier téléversé)."""
    size = size_bytes if size_bytes is not None else (Path(source).stat().st_size if isinstance(source, (str, Path)) else 0)
    report = ZipInspection(filename=filename, size_bytes=size, valid=False)
    if not filename or not _SAFE_UPLOAD_NAME.match(Path(filename).name) or Path(filename).name != filename:
        report.errors.append("Le fichier doit être une archive .zip au nom simple (sans chemin).")
        return report
    rules = _document_rules()
    if hasattr(source, "seek"):
        source.seek(0)
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile:
        report.errors.append("Le fichier n'est pas une archive ZIP lisible (corrompue ou d'un autre format).")
        return report
    except (OSError, ValueError, NotImplementedError) as exc:
        report.errors.append(f"Archive illisible : {type(exc).__name__}")
        return report
    finally:
        if hasattr(source, "seek"):
            source.seek(0)
    report.member_count = len(infos)
    files = [i for i in infos if not i.is_dir()]
    if not files:
        report.errors.append("L'archive est vide.")
        return report
    if len(infos) > rules["max_members"]:
        report.errors.append(f"L'archive contient plus de {rules['max_members']} entrées.")
        return report
    seen, validated = set(), []
    for info in infos:
        reason = _unsafe_member_reason(info)
        if reason:
            report.errors.append("Entrée refusée (" + reason + ").")
            return report
        key = PurePosixPath(info.filename.replace("\\", "/")).as_posix()
        if key in seen:
            report.errors.append(f"Entrées en double : {key}")
            return report
        seen.add(key)
        validated.append((info, PurePosixPath(key)))
    stem = Path(filename).stem
    tops = {p.parts[0] for i, p in validated if p.parts[0] != "__MACOSX" and (i.is_dir() or len(p.parts) >= 2)}
    prefix = (stem,) if tops == {stem} else ()
    report.customer_root = "/".join(prefix)
    customers, targets, encrypted = set(), 0, 0
    per_customer = Counter()
    for info, path in validated:
        if path.parts[0] == "__MACOSX" or tuple(path.parts[:len(prefix)]) != prefix:
            continue
        parts = path.parts[len(prefix):]
        if not parts:
            continue
        if info.is_dir():
            customers.add(parts[0])
            continue
        if len(parts) < 2:
            if _is_target_document(path.name, rules):
                report.errors.append(f"Document cible hors dossier client : {path.as_posix()}")
                return report
            continue
        customers.add(parts[0])
        if _is_target_document(path.name, rules):
            targets += 1
            per_customer[parts[0]] += 1
            encrypted += bool(info.flag_bits & 1)
    if encrypted:
        report.errors.append(f"{encrypted} PDF cible(s) chiffré(s) dans l'archive : non pris en charge.")
        return report
    if not customers:
        report.errors.append("Aucun dossier client trouvé. Structure attendue : <archive>/<ID_CLIENT>/<DOCUMENT>.PDF")
        return report
    if not targets:
        report.errors.append("Aucun document KYC attendu (" + ", ".join(rules["targets"]) + ") dans les dossiers clients.")
        return report
    report.customer_count, report.target_pdf_count = len(customers), targets
    without = len(customers) - len(per_customer)
    if without:
        report.warnings.append(f"{without} dossier(s) client sans aucun document cible (signalés comme manquants).")
    report.valid = True
    return report


def full_integrity_check(zip_path: Path) -> None:
    """Contrôle CRC complet, exécuté au lancement (lecture intégrale de l'archive)."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad = archive.testzip()
    except (zipfile.BadZipFile, OSError) as exc:
        raise InvalidZipError(f"Archive illisible : {exc}") from exc
    if bad is not None:
        raise InvalidZipError(f"CRC invalide pour {bad!r}", "L'archive ZIP est corrompue (contrôle CRC en échec).")


def persist_upload(fileobj: BinaryIO, filename: str, input_dir: Path) -> Path:
    """Écriture atomique : fichier temporaire → fsync → contrôle → renommage atomique.

    Le nom d'origine est conservé : le notebook déduit la racine clients du nom de l'archive
    (dossier racine identique au nom du ZIP). Appelé uniquement sous verrou de traitement.
    """
    input_dir.mkdir(parents=True, exist_ok=True)
    if not _SAFE_UPLOAD_NAME.match(filename) or Path(filename).name != filename:
        raise InvalidZipError(f"Nom refusé : {filename!r}", "Le nom de l'archive n'est pas valide.")
    temporary = input_dir / f".upload-{uuid.uuid4().hex}.part"
    final = input_dir / filename
    try:
        fileobj.seek(0)
        with temporary.open("xb") as target:
            shutil.copyfileobj(fileobj, target, COPY_CHUNK)
            target.flush()
            os.fsync(target.fileno())
        inspection = inspect_zip(temporary, filename)
        if not inspection.valid:
            raise InvalidZipError("; ".join(inspection.errors), "L'archive ZIP n'est pas valide : " + " ".join(inspection.errors))
        for previous in input_dir.glob("*"):
            # Seuls les ZIP d'entrée précédents sont remplacés ; aucun checkpoint n'est ici.
            if previous.is_file() and previous.suffix.lower() == ".zip" and previous.name != filename:
                previous.unlink()
        os.replace(temporary, final)
        _fsync_directory(input_dir)
    finally:
        temporary.unlink(missing_ok=True)
    return final


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(COPY_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- disponibilité de l'environnement (sans charger torch ni le modèle) -------------------
@dataclass
class ReadinessCheck:
    name: str
    ok: bool
    blocking: bool
    detail: str


def check_writable(directory: Path) -> tuple[bool, str]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".write-test-", delete=True) as probe:
            probe.write(b"ok")
            probe.flush()
        return True, str(directory)
    except OSError as exc:
        return False, f"{directory} : {exc.strerror or exc}"


def readiness(cfg: AppConfig) -> list[ReadinessCheck]:
    checks = []
    for label, directory in (("Sorties pipeline", cfg.output_root), ("Dossier d'entrée", cfg.input_dir),
                             ("Dossier d'extraction", cfg.extract_dir)):
        ok, detail = check_writable(directory)
        checks.append(ReadinessCheck(label + " accessibles en écriture", ok, True, detail))
    checks.append(ReadinessCheck("Fichier de référence présent", cfg.reference_csv.is_file(), True, str(cfg.reference_csv)))
    model_ok = (cfg.model_path / "config.json").is_file()
    checks.append(ReadinessCheck("Dépôt modèle Qwen3.8 local présent", model_ok, True, str(cfg.model_path)))
    try:
        manifest = load_engine_manifest()
        engine_ok = (cfg.project_root / "engine" / "notebook_core.py").is_file()
        detail = f"notebook {manifest['notebook_sha256'][:12]}…"
        if cfg.notebook_path.is_file():
            current = sha256_file(cfg.notebook_path)
            if current != manifest["notebook_sha256"]:
                engine_ok, detail = False, "notebook modifié depuis la génération : exécuter scripts/build_engine.sh"
    except Exception as exc:  # manifeste absent ou illisible
        engine_ok, detail = False, str(exc)
    checks.append(ReadinessCheck("Moteur généré conforme au notebook", engine_ok, True, detail))
    gpu = gpu_inventory()
    checks.append(ReadinessCheck("GPU NVIDIA visible", bool(gpu["gpus"]), False,
                                 ", ".join(gpu["gpus"]) or gpu["detail"]))
    return checks


_GPU_CACHE: dict = {}


def gpu_inventory() -> dict:
    """Liste des GPU via nvidia-smi (sans importer torch). Résultat mis en cache 5 minutes."""
    import subprocess

    if _GPU_CACHE and time.monotonic() - _GPU_CACHE["at"] < 300:
        return _GPU_CACHE["value"]
    value = {"gpus": [], "detail": "nvidia-smi introuvable"}
    executable = shutil.which("nvidia-smi")
    if executable:
        try:
            output = subprocess.run([executable, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                                    capture_output=True, text=True, timeout=10, check=False)
            names = [line.strip() for line in output.stdout.splitlines() if line.strip()]
            value = {"gpus": names, "detail": "aucun GPU listé" if not names else "ok"}
        except (OSError, subprocess.SubprocessError) as exc:
            value = {"gpus": [], "detail": f"nvidia-smi en échec : {exc}"}
    _GPU_CACHE.update(at=time.monotonic(), value=value)
    return value


# --- métadonnées de run (état durable, indépendant du navigateur) -------------------------
def run_dir(cfg: AppConfig, run_id: str) -> Path:
    return cfg.runs_dir / run_id


def write_run_metadata(cfg: AppConfig, run_id: str, metadata: dict) -> None:
    metadata = {**metadata, "run_id": run_id, "metadata_updated_at": utc_now()}
    atomic_write_json(run_dir(cfg, run_id) / "run.json", metadata)
    atomic_write_json(cfg.runs_dir / "last_run.json", metadata)


def read_last_run(cfg: AppConfig) -> dict | None:
    path = cfg.runs_dir / "last_run.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
