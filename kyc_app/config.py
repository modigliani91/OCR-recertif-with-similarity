"""Configuration centrale de l'application KYC (Domino Data Lab).

Tous les chemins sont définis ici et peuvent être surchargés par variables d'environnement
(Domino : Project settings → Environment variables, ou export dans app.sh).
Les règles métier (versions, seuil 75 %, familles de documents, chemin du modèle par défaut)
ne sont PAS redéfinies ici : elles proviennent du notebook via engine/engine_manifest.json.

Variables d'environnement reconnues :
    KYC_OUTPUT_ROOT        racine des sorties du pipeline       (défaut /mnt/output_qwen38)
    KYC_APP_ROOT           racine de travail de l'application   (défaut /mnt/kyc_app)
    KYC_INPUT_DIR          ZIP téléversé                         (défaut $KYC_APP_ROOT/input)
    KYC_EXTRACT_DIR        extractions ZIP sécurisées            (défaut $KYC_APP_ROOT/extracted)
    KYC_REFERENCE_CSV      fichier de référence                  (défaut : recherche, voir REFERENCE_CANDIDATES)
    KYC_MODEL_PATH         dépôt Qwen3.8 local                   (défaut : chemin exact du notebook)
    KYC_RUN_MODE           FULL_PIPELINE (défaut) | DIAGNOSTIC (smoke test, 1 client)
    KYC_DIAGNOSTIC_CUSTOMER_COUNT  nombre de clients en DIAGNOSTIC (défaut 1)
    KYC_LOCK_STALE_AFTER_S verrou considéré orphelin sans battement (défaut 900 s)
    KYC_MAX_UPLOAD_MB      taille maximale du ZIP téléversé       (défaut 4096)
"""
from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass
from pathlib import Path

APP_VERSION = "1.0.0"
APP_TITLE = "KYC Recertification — AI Document Control"
PROJECT_ROOT = Path(__file__).resolve().parent
NOTEBOOK_FILENAME = "kyc_qwen38_production_kyc_pipeline_name_similarity_v2.ipynb"
ENGINE_MANIFEST_PATH = PROJECT_ROOT / "engine" / "engine_manifest.json"
REPORT_FILENAME = "kyc_recertification_control.xlsx"
DEFAULT_INPUT_ZIP_NAME = "kyc_documents_test.zip"
REFERENCE_FILENAME = "recertication_lissage_updated.csv"
ALLOWED_RUN_MODES = ("FULL_PIPELINE", "DIAGNOSTIC")


class ConfigurationError(RuntimeError):
    """Configuration de l'application incohérente (indépendante d'un traitement)."""


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} doit être un entier : {value!r}") from exc


@functools.lru_cache(maxsize=1)
def load_engine_manifest() -> dict:
    """Manifeste écrit par engine/build_engine.py : versions et règles lues dans le notebook."""
    if not ENGINE_MANIFEST_PATH.is_file():
        raise ConfigurationError(
            "engine/engine_manifest.json absent : exécuter `bash scripts/build_engine.sh` depuis la racine du projet.")
    return json.loads(ENGINE_MANIFEST_PATH.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    notebook_path: Path
    output_root: Path
    report_path: Path
    app_root: Path
    input_dir: Path
    extract_dir: Path
    reference_csv: Path
    reference_candidates: tuple[Path, ...]
    reference_from_env: bool
    model_path: Path
    model_path_from_env: bool
    run_mode: str
    diagnostic_customer_count: int
    lock_path: Path
    runs_dir: Path
    logs_dir: Path
    app_log_path: Path
    lock_stale_after_s: int
    heartbeat_interval_s: int
    max_upload_mb: int

    @property
    def default_input_zip_path(self) -> Path:
        return self.input_dir / DEFAULT_INPUT_ZIP_NAME

    def engine_overrides(self) -> dict:
        """Surcharges passées à StructuredConfig du notebook (uniquement chemins et mode d'exécution).

        Le chemin du modèle n'est transmis que si KYC_MODEL_PATH est défini : sinon la valeur exacte
        du notebook est conservée. Aucune règle métier, seuil ou prompt n'est surchargé.
        """
        overrides = {
            "OUTPUT_DIR": self.output_root,
            "ZIP_PATH": self.default_input_zip_path,
            "EXTRACT_DIR": self.extract_dir,
            "REFERENCE_CSV": self.reference_csv,
            "RUN_MODE": self.run_mode,
            "INPUT_SOURCE": "ZIP",
        }
        if self.model_path_from_env:
            overrides["MODEL_PATH"] = self.model_path
        if self.run_mode == "DIAGNOSTIC":
            overrides["DIAGNOSTIC_CUSTOMER_COUNT"] = self.diagnostic_customer_count
        return overrides

    def describe(self) -> dict:
        return {
            "APP_VERSION": APP_VERSION,
            "OUTPUT_ROOT": str(self.output_root),
            "REPORT_PATH": str(self.report_path),
            "INPUT_DIR": str(self.input_dir),
            "EXTRACT_DIR": str(self.extract_dir),
            "REFERENCE_CSV": str(self.reference_csv),
            "MODEL_PATH": str(self.model_path),
            "RUN_MODE": self.run_mode,
            "LOCK_PATH": str(self.lock_path),
            "APP_LOG": str(self.app_log_path),
        }


@functools.lru_cache(maxsize=1)
def load_config() -> AppConfig:
    manifest = load_engine_manifest()
    defaults = manifest["config_defaults"]
    output_root = _env_path("KYC_OUTPUT_ROOT", Path("/mnt/output_qwen38"))
    app_root = _env_path("KYC_APP_ROOT", Path("/mnt/kyc_app"))
    candidates = (
        Path("/mnt") / REFERENCE_FILENAME,
        Path("/mnt/recertification_lissage_updated.csv"),  # variante orthographique citée par le notebook
        PROJECT_ROOT / REFERENCE_FILENAME,
    )
    reference_env = os.environ.get("KYC_REFERENCE_CSV", "").strip()
    if reference_env:
        reference = Path(reference_env).expanduser()
    else:
        reference = next((c for c in candidates if c.is_file()), candidates[0])
    model_env = os.environ.get("KYC_MODEL_PATH", "").strip()
    model_path = Path(model_env).expanduser() if model_env else Path(defaults["MODEL_PATH"])
    run_mode = os.environ.get("KYC_RUN_MODE", "FULL_PIPELINE").strip().upper()
    if run_mode not in ALLOWED_RUN_MODES:
        raise ConfigurationError(f"KYC_RUN_MODE doit valoir {' ou '.join(ALLOWED_RUN_MODES)} : {run_mode!r}")
    logs_dir = output_root / "logs"
    stale = _env_int("KYC_LOCK_STALE_AFTER_S", 900)
    heartbeat = _env_int("KYC_LOCK_HEARTBEAT_S", 30)
    if heartbeat * 4 > stale:
        raise ConfigurationError("KYC_LOCK_STALE_AFTER_S doit valoir au moins 4 × KYC_LOCK_HEARTBEAT_S.")
    return AppConfig(
        project_root=PROJECT_ROOT,
        notebook_path=PROJECT_ROOT / NOTEBOOK_FILENAME,
        output_root=output_root,
        report_path=output_root / "reports" / REPORT_FILENAME,
        app_root=app_root,
        input_dir=_env_path("KYC_INPUT_DIR", app_root / "input"),
        extract_dir=_env_path("KYC_EXTRACT_DIR", app_root / "extracted"),
        reference_csv=reference,
        reference_candidates=candidates,
        reference_from_env=bool(reference_env),
        model_path=model_path,
        model_path_from_env=bool(model_env),
        run_mode=run_mode,
        diagnostic_customer_count=max(1, _env_int("KYC_DIAGNOSTIC_CUSTOMER_COUNT", 1)),
        lock_path=output_root / ".kyc_processing.lock",
        runs_dir=output_root / "app_runs",
        logs_dir=logs_dir,
        app_log_path=logs_dir / "kyc_app.log",
        lock_stale_after_s=stale,
        heartbeat_interval_s=heartbeat,
        max_upload_mb=_env_int("KYC_MAX_UPLOAD_MB", 4096),
    )


def pipeline_versions() -> dict:
    """Versions affichées dans « Détails techniques » (lues dans le notebook, pas recopiées)."""
    manifest = load_engine_manifest()
    defaults = manifest["config_defaults"]
    return {
        "APP_VERSION": APP_VERSION,
        "PIPELINE_VERSION": defaults["PIPELINE_VERSION"],
        "RAW_OCR_VERSION": defaults["RAW_OCR_VERSION"],
        "STRUCTURED_EXTRACTION_VERSION": defaults["STRUCTURED_EXTRACTION_VERSION"],
        "MATCHING_VERSION": defaults["MATCHING_VERSION"],
        "NAME_SIMILARITY_THRESHOLD": defaults["HANDWRITTEN_NAME_SIMILARITY_THRESHOLD"],
        "NOTEBOOK_SHA256": manifest["notebook_sha256"],
        "ENGINE_BUILD": manifest["generated_at"],
    }


# Constantes explicites demandées (lecture seule ; la source unique reste load_config()).
_CFG = load_config()
OUTPUT_ROOT: Path = _CFG.output_root
REPORT_PATH: Path = _CFG.report_path
INPUT_DIR: Path = _CFG.input_dir
INPUT_ZIP_PATH: Path = _CFG.default_input_zip_path  # nom réel conservé si l'archive téléversée porte un autre nom
REFERENCE_CSV: Path = _CFG.reference_csv
MODEL_PATH: Path = _CFG.model_path
