"""Accès unique au moteur KYC issu du notebook.

``load_engine()`` importe ``engine.notebook_core`` UNE seule fois par processus (cache
``sys.modules`` de Python), vérifie qu'il correspond bien au notebook de référence, puis
installe l'instrumentation de progression. Le modèle Qwen vit dans les globales de ce
module (``model``, ``processor``, ``MODEL_READY``) : c'est l'unique propriétaire du modèle.

``reset_run_state()`` remet à zéro l'état « par session » que la cellule ``paths`` du notebook
crée au démarrage d'un kernel, sans toucher au modèle déjà chargé.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from config import AppConfig, load_engine_manifest

from . import instrumentation

ENGINE_DIR = Path(__file__).resolve().parent
CORE_FILE = ENGINE_DIR / "notebook_core.py"
_IMPORT_LOCK = threading.Lock()

# Globales créées par la cellule « paths » (§4) et réinitialisées pour chaque traitement.
PER_RUN_RESET_NAMES = (
    "SESSION_ID", "SESSION_START", "ERRORS", "ATTEMPTS", "PERFORMANCE", "REFERENCE_REPAIRS",
    "CUSTOMER_SNAPSHOTS", "CUSTOMER_RESULTS", "RAW_CACHE_INDEX", "RUN_DATE", "DOCUMENT_WRITES",
    "RUNTIME_STATE", "RAW_MODEL_INFO", "MODEL_REPOSITORY_SIGNATURE", "RAW_RUN_SUMMARY",
    "STRUCTURE_RUN_SUMMARY", "MATCH_RUN_SUMMARY",
)
# Globales créées par les cellules d'exécution : absentes d'un kernel neuf, donc supprimées
# avant chaque traitement pour qu'aucun résultat d'un run précédent ne puisse être relu.
STAGE_OUTPUT_NAMES = (
    "ALL_CUSTOMERS", "INVENTORY", "SELECTED_DOCUMENTS", "CUSTOMERS_SELECTED", "SELECTIONS_BY_CUSTOMER",
    "selection", "OUTPUT_POPULATION", "SELECTED_CUSTOMER_IDS", "BENCHMARK_REPORT", "REFERENCE",
    "REPORT_SUMMARY", "AUDIT", "STRUCTURE_MODEL_SIGNATURE",
)


class EngineIntegrityError(RuntimeError):
    """Le module généré ne correspond plus au notebook ou au manifeste."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_integrity(cfg: AppConfig) -> dict:
    manifest = load_engine_manifest()
    if not CORE_FILE.is_file():
        raise EngineIntegrityError("engine/notebook_core.py absent : exécuter scripts/build_engine.sh")
    core_text = CORE_FILE.read_text(encoding="utf-8")
    if hashlib.sha256(core_text.encode("utf-8")).hexdigest() != manifest["core_sha256"]:
        raise EngineIntegrityError("engine/notebook_core.py a été modifié à la main : régénérer avec scripts/build_engine.sh")
    if cfg.notebook_path.is_file() and _sha256(cfg.notebook_path) != manifest["notebook_sha256"]:
        raise EngineIntegrityError("Le notebook a changé depuis la génération du moteur : exécuter scripts/build_engine.sh")
    return manifest


def is_loaded() -> bool:
    import sys

    return "engine.notebook_core" in sys.modules and getattr(sys.modules["engine.notebook_core"], "_APP_INSTRUMENTED", False)


def load_engine(cfg: AppConfig):
    """Importe le moteur une fois (imports lourds : torch, transformers) et l'instrumente."""
    with _IMPORT_LOCK:
        if is_loaded():
            import sys

            return sys.modules["engine.notebook_core"]
        verify_integrity(cfg)
        core = importlib.import_module("engine.notebook_core")
        _verify_configuration(core, cfg)
        instrumentation.install(core)
        return core


def _verify_configuration(core, cfg: AppConfig) -> None:
    """Les chemins du moteur et ceux de l'application doivent coïncider exactement."""
    expected_report = core.DIRS["reports"] / "kyc_recertification_control.xlsx"
    problems = []
    if Path(core.CFG.OUTPUT_DIR) != cfg.output_root:
        problems.append(f"OUTPUT_DIR moteur {core.CFG.OUTPUT_DIR} ≠ {cfg.output_root}")
    if expected_report != cfg.report_path:
        problems.append(f"rapport moteur {expected_report} ≠ {cfg.report_path}")
    if Path(core.CFG.REFERENCE_CSV) != cfg.reference_csv:
        problems.append("REFERENCE_CSV incohérent")
    if core.CFG.RUN_MODE != cfg.run_mode:
        problems.append("RUN_MODE incohérent")
    if problems:
        raise EngineIntegrityError("Configuration moteur/application incohérente : " + " ; ".join(problems))


def reset_run_state(core, zip_path: Path) -> str:
    """Reproduit l'état d'un kernel neuf pour un nouveau traitement, modèle conservé.

    Lignes reprises de la cellule « paths » (§4) et de la création du budget (§18).
    ``RUNTIME_STATE['fatal_cuda']`` est conservé : un contexte CUDA corrompu exige un
    redémarrage de l'application, exactement comme un redémarrage de kernel dans le notebook.
    """
    previous_fatal = bool(getattr(core, "RUNTIME_STATE", {}).get("fatal_cuda", False))
    core.SESSION_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    core.SESSION_START = time.perf_counter()
    core.ERRORS, core.ATTEMPTS, core.PERFORMANCE, core.REFERENCE_REPAIRS = [], [], [], []
    core.CUSTOMER_SNAPSHOTS, core.CUSTOMER_RESULTS = {}, {}
    core.RAW_CACHE_INDEX = defaultdict(list)
    core.RUN_DATE = date.today()
    core.DOCUMENT_WRITES = set()
    core.RUNTIME_STATE = {"fatal_cuda": previous_fatal, "persistence_failed": False}
    core.RAW_MODEL_INFO = {}
    core.MODEL_REPOSITORY_SIGNATURE = None
    core.RAW_RUN_SUMMARY = {}
    core.STRUCTURE_RUN_SUMMARY = {}
    core.MATCH_RUN_SUMMARY = {}
    core.BUDGET = core.CallBudget(core.CFG.MAX_QWEN_CALLS if core.CFG.MAX_QWEN_CALLS is not None else 10**12)
    core.BUDGET_SESSION_ID = core.SESSION_ID
    core.STRUCTURE_DOCUMENT_CONTEXT.clear()
    core._RENDER_CACHE.clear()
    for name in STAGE_OUTPUT_NAMES:
        if hasattr(core, name):
            delattr(core, name)
    # Seul champ propre au run : l'archive téléversée (nom d'origine conservé pour la détection
    # de la racine clients du notebook). Aucun champ entrant dans une empreinte n'est modifié.
    core.CFG = dataclasses.replace(core.CFG, ZIP_PATH=Path(zip_path))
    return core.SESSION_ID


def engine_summary() -> dict:
    manifest = load_engine_manifest()
    return {"notebook_sha256": manifest["notebook_sha256"], "core_sha256": manifest["core_sha256"],
            "generated_at": manifest["generated_at"], "code_cells": manifest["code_cell_count"],
            "stage_functions": manifest["stage_functions"]}


def manifest_json() -> str:
    return json.dumps(load_engine_manifest(), ensure_ascii=False, indent=2)
