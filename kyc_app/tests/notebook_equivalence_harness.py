"""Équivalence NOTEBOOK ↔ APPLICATION sans GPU (Qwen simulé identique des deux côtés).

1. « notebook » : exécute les cellules de code du fichier .ipynb d'origine, dans l'ordre, dans un
   espace de noms unique (comme un kernel Jupyter), avec uniquement la configuration modifiée
   (OUTPUT_DIR, ZIP_PATH, EXTRACT_DIR, REFERENCE_CSV, MODEL_PATH, RUN_MODE=FULL_PIPELINE) ;
2. « app » : exécute ``pipeline.run_kyc_pipeline`` (moteur généré + orchestration applicative) ;
3. compare les deux répertoires de sortie avec tools/regression_compare.py.

    python tests/notebook_equivalence_harness.py --workdir /tmp/kyc_equivalence

Le Qwen simulé rend les résultats métier non représentatifs ; le test prouve que la conversion
(extraction du code, orchestration, configuration) ne change pas les sorties à entrées égales.
Pour la validation réelle sur GPU, voir README § « Validation de non-régression ».
"""
from __future__ import annotations

import __future__
import argparse
import json
import os
import subprocess
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
EXECUTION_CELLS = {"4869d7f3", "1687acc2", "5eb70c68"}


class NamespaceProxy:
    """Expose un dict d'exécution comme un module pour réutiliser install_fake_qwen."""

    def __init__(self, namespace: dict):
        object.__setattr__(self, "_ns", namespace)

    def __getattr__(self, name):
        try:
            return self._ns[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self._ns[name] = value


def run_notebook(workdir: Path, data: dict) -> None:
    from fake_qwen import install_fake_qwen

    try:
        import IPython.display  # noqa: F401
    except ImportError:  # environnement minimal : même repli que le moteur généré
        package, display = types.ModuleType("IPython"), types.ModuleType("IPython.display")
        display.display = print
        package.display = display
        sys.modules.update({"IPython": package, "IPython.display": display})
    notebook = json.loads((PROJECT_ROOT / "kyc_qwen38_production_kyc_pipeline_name_similarity_v2.ipynb").read_text(encoding="utf-8"))
    overrides = {"OUTPUT_DIR": workdir / "output_notebook", "ZIP_PATH": data["zip"],
                 "EXTRACT_DIR": workdir / "extract_notebook", "REFERENCE_CSV": data["reference"],
                 "MODEL_PATH": data["model"], "RUN_MODE": "FULL_PIPELINE"}
    namespace = {"__name__": "__main__", "__REGRESSION_OVERRIDES__": overrides}
    flags = __future__.annotations.compiler_flag  # IPython conserve l'import __future__ entre cellules
    for cell in (c for c in notebook["cells"] if c["cell_type"] == "code"):
        source = "".join(cell["source"])
        if cell.get("id") == "e7602a7f":
            assert source.count("CFG = StructuredConfig()") == 1
            source = source.replace("CFG = StructuredConfig()", "CFG = StructuredConfig(**__REGRESSION_OVERRIDES__)")
        if cell.get("id") in EXECUTION_CELLS:
            install_fake_qwen(NamespaceProxy(namespace))
        exec(compile(source, f"<cell {cell.get('id')}>", "exec", flags=flags, dont_inherit=True), namespace)
    print("NOTEBOOK RUN OK", namespace["MATCH_RUN_SUMMARY"].get("counts"))


def run_app(workdir: Path, data: dict) -> None:
    os.environ.update({"KYC_OUTPUT_ROOT": str(workdir / "output_app"), "KYC_APP_ROOT": str(workdir / "app"),
                       "KYC_REFERENCE_CSV": str(data["reference"]), "KYC_MODEL_PATH": str(data["model"]),
                       "KYC_RUN_MODE": "FULL_PIPELINE"})
    sys.path.insert(0, str(PROJECT_ROOT))
    from config import load_config
    from fake_qwen import install_fake_qwen
    from file_manager import persist_upload
    from logging_config import configure_logging
    from pipeline import run_kyc_pipeline
    import engine

    cfg = load_config()
    configure_logging(cfg)
    install_fake_qwen(engine.load_engine(cfg))
    with data["zip"].open("rb") as handle:
        zip_path = persist_upload(handle, data["zip"].name, cfg.input_dir)
    result = run_kyc_pipeline(zip_path, cfg=cfg)
    assert result.success, result.to_dict()
    print("APP RUN OK", result.status, result.conform_count, result.non_conform_count, result.recheck_count)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--mode", choices=("both", "notebook", "app"), default="both")
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    from synthetic_data import build_dataset

    data = build_dataset(workdir / "data")
    if args.mode == "notebook":
        run_notebook(workdir, data)
        return 0
    if args.mode == "app":
        run_app(workdir, data)
        return 0
    for mode in ("notebook", "app"):  # processus séparés : configuration figée à l'import
        code = subprocess.run([sys.executable, __file__, "--workdir", str(workdir), "--mode", mode],
                              env=os.environ.copy(), check=False)
        if code.returncode:
            print(f"ÉCHEC exécution {mode}", file=sys.stderr)
            return code.returncode
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "tools" / "regression_compare.py"),
                           "--reference", str(workdir / "output_notebook"), "--candidate", str(workdir / "output_app"),
                           "--json", str(workdir / "comparison.json")], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
