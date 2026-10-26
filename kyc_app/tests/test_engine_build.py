"""Le moteur généré reste conforme au notebook : copie fidèle, état par run couvert, fonctions instrumentées présentes."""
import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "engine" / "notebook_core.py"
NOTEBOOK = ROOT / "kyc_qwen38_production_kyc_pipeline_name_similarity_v2.ipynb"


def test_build_check_passes():
    result = subprocess.run([sys.executable, str(ROOT / "engine" / "build_engine.py"), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_unchanged_cell_is_verbatim():
    core = CORE.read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "engine" / "engine_manifest.json").read_text(encoding="utf-8"))
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    sources = {c.get("id"): "".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"}
    kept = [c["id"] for c in manifest["cells"] if c["action"] == "keep"]
    assert len(kept) == 41
    for cell_id in kept:
        assert sources[cell_id].rstrip("\n") in core, cell_id


def test_per_run_state_is_reset():
    from engine import PER_RUN_RESET_NAMES

    manifest = json.loads((ROOT / "engine" / "engine_manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["per_run_state_names"]) <= set(PER_RUN_RESET_NAMES)


def test_instrumented_and_stage_functions_exist():
    from engine.instrumentation import INSTRUMENTED_FUNCTIONS

    tree = ast.parse(CORE.read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in (*INSTRUMENTED_FUNCTIONS, "_nb_stage_inventory", "_nb_stage_pass_a", "_nb_stage_pass_b",
                 "_nb_stage_matching_and_reports", "ensure_model_loaded", "run_name_similarity_tests"):
        assert name in defined, name


def test_business_rules_read_from_notebook():
    from config import pipeline_versions

    versions = pipeline_versions()
    assert versions["NAME_SIMILARITY_THRESHOLD"] == 75.0
    assert versions["MATCHING_VERSION"] == "KYC_MATCHING_HANDWRITTEN_NAME_SIM_75_V1"
