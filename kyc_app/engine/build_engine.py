"""Génère ``engine/notebook_core.py`` à partir du notebook validé (source de vérité).

Principe : AUCUNE fonction du notebook n'est réécrite à la main. Les 47 cellules de code
sont recopiées octet pour octet, dans l'ordre du notebook, dans un module importable unique
(un seul espace de noms global, comme le kernel Jupyter). Seules transformations, toutes
vérifiées par AST et listées dans ``engine_manifest.json`` :

1. cellule ``config``  : ``CFG = StructuredConfig()`` devient
   ``CFG = StructuredConfig(**_APP_ENGINE_OVERRIDES)`` (chemins Domino, RUN_MODE) ;
2. cellule ``imports`` : ``from IPython.display import display`` reçoit un repli texte
   si IPython est absent (affichage diagnostic uniquement) ;
3. cellules d'EXÉCUTION (inventaire, Pass A, Pass B, matching+rapports) : leurs
   instructions exactes sont placées dans des fonctions ``_nb_stage_*`` avec une
   déclaration ``global`` des noms qu'elles assignent, afin que l'application les
   déclenche à la demande au lieu de les exécuter à l'import.

Usage :
    python -m engine.build_engine                # (re)génère le module et le manifeste
    python -m engine.build_engine --check        # échoue si les fichiers ne correspondent plus au notebook
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import sys
import tokenize
from datetime import datetime, timezone
from pathlib import Path

BUILD_TOOL_VERSION = "1.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NOTEBOOK = PROJECT_ROOT / "kyc_qwen38_production_kyc_pipeline_name_similarity_v2.ipynb"
ENGINE_DIR = Path(__file__).resolve().parent
CORE_PATH = ENGINE_DIR / "notebook_core.py"
MANIFEST_PATH = ENGINE_DIR / "engine_manifest.json"

# Plan exhaustif et ordonné des cellules de code. Toute divergence (cellule ajoutée,
# supprimée, déplacée ou renommée) arrête la génération : une revue humaine est requise.
# Actions : keep | config | imports | split:<fonction> | wrap:<fonction>
CELL_PLAN: list[tuple[str, str, str]] = [
    ("e7602a7f", "config", "config"),
    ("927d0e7c", "packages", "keep"),
    ("fedf7d9f", "imports", "imports"),
    ("0ebb79df", "business_rules", "keep"),
    ("fde43ba6", "paths", "keep"),
    ("329f3844", "io_helpers", "keep"),
    ("ba81ff85", "normalization", "keep"),
    ("6f9e2a7c", "runtime_support", "keep"),
    ("d6ddd28a", "quality_engine", "keep"),
    ("d6113d1f", "zip_helpers", "keep"),
    ("df68eafe", "inventory_helpers", "keep"),
    ("a98d9d6a", "discovery", "split:_nb_stage_inventory"),
    ("6caaf980", "image_helpers", "keep"),
    ("c84ece7a", "raw_prompt", "keep"),
    ("98fea477", "template_helpers", "keep"),
    ("e03d63da", "model_diagnostics", "keep"),
    ("e99bd894", "model_loading", "keep"),
    ("a974ba61", "raw_validation", "keep"),
    ("2db39b5e", "qwen_inference", "keep"),
    ("81a72a5f", "raw_store", "keep"),
    ("8ded3d7f", "raw_runner", "keep"),
    ("9612c204", "raw_benchmark", "keep"),
    ("4869d7f3", "exécution_pass_a", "wrap:_nb_stage_pass_a"),
    ("38a4e122", "evidence_contract", "keep"),
    ("be44a34b", "structured_text_rules", "keep"),
    ("5172d273", "document_detection", "keep"),
    ("07e82e54", "structured_adapter", "keep"),
    ("6b41cad4", "model_confidence", "keep"),
    ("2603890e", "text_inference", "keep"),
    ("d4ccbde5", "structured_vision_inference", "keep"),
    ("754eccd0", "structured_image_attempt", "keep"),
    ("306d7bd7", "structured_cascade", "keep"),
    ("5df58302", "matching", "keep"),
    ("c1727ff0", "document_validators", "keep"),
    ("11135bf2", "aggregation", "keep"),
    ("6e8122b7", "document_json", "keep"),
    ("289c636d", "structured_runner", "keep"),
    ("1687acc2", "exécution_pass_b", "wrap:_nb_stage_pass_b"),
    ("10121a1e", "reference_helpers", "keep"),
    ("similarity-v2-code", "name_similarity", "keep"),
    ("ecca80c3", "reference_consistency", "keep"),
    ("similarity-tests-code", "name_similarity_tests", "keep"),
    ("74824fff", "customer_runner", "keep"),
    ("ba3fdbd3", "excel_helpers", "keep"),
    ("similarity-report-code", "name_similarity_reporting", "keep"),
    ("fc556498", "reporting", "keep"),
    ("5eb70c68", "exécution_matching_et_rapports", "wrap:_nb_stage_matching_and_reports"),
]

CONFIG_ORIGINAL = "CFG = StructuredConfig()"
CONFIG_REPLACEMENT = "CFG = StructuredConfig(**_APP_ENGINE_OVERRIDES)  # [app] surcharges Domino (config.py)"
FUTURE_LINE = "from __future__ import annotations"
CONFIG_PRELUDE = (
    "# [app] Seul ajout de la cellule config : surcharges de chemins/mode issues de config.py.\n"
    "import config as _kyc_app_config\n"
    "_APP_ENGINE_OVERRIDES = _kyc_app_config.load_config().engine_overrides()\n"
)
IPYTHON_ORIGINAL = "from IPython.display import display"
IPYTHON_REPLACEMENT = (
    "try:\n"
    "    from IPython.display import display\n"
    "except ImportError:  # [app] IPython absent hors Jupyter : affichage diagnostic textuel équivalent.\n"
    "    def display(obj):\n"
    "        print(obj)"
)
# Noms d'état d'exécution assignés par la cellule paths, que l'application réinitialise à chaque run.
PATHS_CELL_ID = "fde43ba6"
PROCESS_LEVEL_NAMES = {"DIRS", "directory", "logger", "handler", "MODEL_READY"}
CONFIG_KEYS_FOR_MANIFEST = [
    "PIPELINE_VERSION", "RAW_OCR_VERSION", "STRUCTURED_EXTRACTION_VERSION", "SCHEMA_VERSION",
    "MATCHING_VERSION", "HANDWRITTEN_NAME_SIMILARITY_THRESHOLD", "CONFIDENCE_VERSION",
    "DOCUMENT_JSON_SCHEMA_VERSION", "CUSTOMER_JSON_SCHEMA_VERSION", "MODEL_PATH", "RUN_MODE",
    "TARGET_DOCUMENTS", "DOCUMENT_ALIASES", "RECOGNIZE_DUPLICATE_SUFFIX", "MAX_ZIP_MEMBERS",
    "MAX_PDF_BYTES", "MAX_COMPRESSION_RATIO", "MAX_EXTRACT_BYTES", "DUPLICATE_POLICY",
]


class BuildError(RuntimeError):
    """La génération ne peut pas garantir une copie fidèle du notebook."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cells(notebook_path: Path) -> tuple[list[dict], str]:
    raw = notebook_path.read_bytes()
    notebook = json.loads(raw.decode("utf-8"))
    cells, last_title = [], None
    for cell in notebook["cells"]:
        source = "".join(cell["source"])
        if cell["cell_type"] == "markdown":
            first = next((line for line in source.splitlines() if line.startswith("#")), None)
            if first:
                last_title = first.lstrip("# ").strip()
        elif cell["cell_type"] == "code":
            cells.append({"id": cell.get("id"), "tags": cell.get("metadata", {}).get("tags", []),
                          "source": source, "title": last_title})
    return cells, hashlib.sha256(raw).hexdigest()


def assigned_names(statements: list[ast.stmt]) -> list[str]:
    """Noms stockés au niveau de la cellule (y compris dans if/for), hors fonctions imbriquées."""
    names: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                             ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return  # portées propres : leurs variables ne sont pas des globales de la cellule
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id not in names:
            names.append(node.id)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in statements:
        visit(statement)
    return names


def reject_multiline_strings(code: str, cell_id: str) -> None:
    """L'indentation ajoutée ne doit jamais modifier le contenu d'une chaîne littérale."""
    string_types = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
    for token in tokenize.generate_tokens(io.StringIO(code).readline):
        if token.type in string_types and token.start[0] != token.end[0]:
            raise BuildError(f"Chaîne multiligne dans le code à encapsuler ({cell_id}) : indentation non sûre.")


def wrap_statements(code: str, function_name: str, cell_id: str, tag: str) -> str:
    tree = ast.parse(code)
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in tree.body):
        raise BuildError(f"Cellule d'exécution {cell_id} contenant des définitions : plan à revoir.")
    reject_multiline_strings(code, cell_id)
    names = assigned_names(tree.body)
    lines = [f"def {function_name}():",
             f'    """[app] Instructions exactes de la cellule {cell_id} ({tag}), exécutées à la demande."""']
    if names:
        lines.append("    global " + ", ".join(names))
    lines.extend(("    " + line) if line.strip() else "" for line in code.splitlines())
    wrapped = "\n".join(lines) + "\n"
    # Contrôle AST : le corps de la fonction est exactement la cellule d'origine.
    function = ast.parse(wrapped).body[0]
    body = [s for s in function.body if not isinstance(s, ast.Global)]
    body = body[1:]  # docstring
    if ast.dump(ast.Module(body=body, type_ignores=[])) != ast.dump(tree):
        raise BuildError(f"Encapsulation non fidèle pour {cell_id}")
    return wrapped


def split_definitions(code: str, function_name: str, cell_id: str, tag: str) -> tuple[str, str]:
    tree = ast.parse(code)
    first_exec = None
    for index, statement in enumerate(tree.body):
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
            first_exec = index
            break
    if first_exec is None:
        raise BuildError(f"Cellule {cell_id} : aucune instruction d'exécution à séparer.")
    rest = tree.body[first_exec:]
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in rest):
        raise BuildError(f"Cellule {cell_id} : définitions mélangées aux instructions d'exécution.")
    lines = code.splitlines()
    cut = rest[0].lineno - 1
    definitions = "\n".join(lines[:cut]).rstrip() + "\n"
    execution = "\n".join(lines[cut:]).rstrip() + "\n"
    if ast.dump(ast.parse(definitions)) != ast.dump(ast.Module(body=tree.body[:first_exec], type_ignores=[])):
        raise BuildError(f"Séparation non fidèle pour {cell_id}")
    return definitions, wrap_statements(execution, function_name, cell_id, tag)


def transform_config(code: str) -> str:
    if code.count(CONFIG_ORIGINAL) != 1 or code.count(FUTURE_LINE) != 1:
        raise BuildError("Cellule config inattendue : instanciation CFG ou import __future__ introuvable/ambigu.")
    lines = code.splitlines()
    if lines[0].strip() != FUTURE_LINE:
        raise BuildError("La cellule config doit commencer par l'import __future__.")
    code = code.replace(FUTURE_LINE, FUTURE_LINE + "\n" + CONFIG_PRELUDE.rstrip("\n"), 1)
    return code.replace(CONFIG_ORIGINAL, CONFIG_REPLACEMENT, 1)


def transform_imports(code: str) -> str:
    if code.count(IPYTHON_ORIGINAL) != 1:
        raise BuildError("Import IPython.display attendu une seule fois dans la cellule imports.")
    return code.replace(IPYTHON_ORIGINAL, IPYTHON_REPLACEMENT, 1)


def literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        pass
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Path" and node.args:
        try:
            return str(ast.literal_eval(node.args[0]))
        except (ValueError, TypeError, SyntaxError):
            return None
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "field":
        for keyword in node.keywords:
            if keyword.arg == "default_factory" and isinstance(keyword.value, ast.Lambda):
                try:
                    return ast.literal_eval(keyword.value.body)
                except (ValueError, TypeError, SyntaxError):
                    return None
    return None


def config_defaults(config_code: str) -> dict:
    tree = ast.parse(config_code)
    defaults: dict = {}
    for class_name in ("Config", "StructuredConfig"):
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
        for statement in cls.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value is not None:
                value = literal(statement.value)
                if value is not None:
                    defaults[statement.target.id] = value
    return {key: defaults.get(key) for key in CONFIG_KEYS_FOR_MANIFEST}


def build(notebook_path: Path) -> tuple[str, dict]:
    cells, notebook_sha = load_cells(notebook_path)
    actual = [(c["id"], (c["tags"] or [None])[0]) for c in cells]
    expected = [(cell_id, tag) for cell_id, tag, _ in CELL_PLAN]
    if actual != expected:
        raise BuildError("Les cellules de code du notebook ne correspondent plus au plan de génération.\n"
                         f"Attendu : {expected}\nTrouvé : {actual}")
    parts = [
        "# -*- coding: utf-8 -*-\n"
        "# ============================================================================\n"
        "# FICHIER GÉNÉRÉ — NE PAS MODIFIER À LA MAIN.\n"
        f"# Source : {notebook_path.name}\n"
        f"# SHA-256 notebook : {notebook_sha}\n"
        f"# Générateur : engine/build_engine.py v{BUILD_TOOL_VERSION}\n"
        "# Les cellules de code sont recopiées à l'identique, dans l'ordre du notebook.\n"
        "# Les seules modifications sont balisées « [app] » et listées dans engine_manifest.json.\n"
        "# Pour toute évolution métier : modifier le notebook puis relancer scripts/build_engine.sh.\n"
        "# ============================================================================\n"
    ]
    manifest_cells, stage_functions = [], []
    config_code = None
    for index, (cell, (cell_id, tag, action)) in enumerate(zip(cells, CELL_PLAN), 1):
        source = cell["source"]
        header = f"\n# ---- [cellule {index}/{len(cells)} | id={cell_id} | tag={tag}] {cell['title'] or ''} ----\n"
        if action == "config":
            config_code = source
            body = transform_config(source)
            # L'en-tête de la première cellule reste un commentaire, l'import __future__ reste la 1re instruction.
        elif action == "imports":
            body = transform_imports(source)
        elif action == "keep":
            body = source
        elif action.startswith("split:"):
            function_name = action.split(":", 1)[1]
            definitions, wrapped = split_definitions(source, function_name, cell_id, tag)
            body = definitions + "\n\n" + wrapped
            stage_functions.append(function_name)
        elif action.startswith("wrap:"):
            function_name = action.split(":", 1)[1]
            body = wrap_statements(source, function_name, cell_id, tag)
            stage_functions.append(function_name)
        else:
            raise BuildError("Action inconnue : " + action)
        parts.append(header + body.rstrip("\n") + "\n")
        manifest_cells.append({"index": index, "id": cell_id, "tag": tag, "action": action,
                               "title": cell["title"], "source_sha256": sha256_text(source),
                               "source_lines": len(source.splitlines())})
    generated = "".join(parts)
    compile(generated, str(CORE_PATH), "exec")
    tree = ast.parse(generated)
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    missing = [name for name in stage_functions if name not in defined]
    if missing:
        raise BuildError("Fonctions d'étape manquantes : " + ", ".join(missing))
    for cell, (cell_id, _, action) in zip(cells, CELL_PLAN):
        if action == "keep" and cell["source"].rstrip("\n") not in generated:
            raise BuildError(f"Cellule {cell_id} non recopiée à l'identique")
    paths_cell = next(c for c in cells if c["id"] == PATHS_CELL_ID)
    run_state = [n for n in assigned_names(ast.parse(paths_cell["source"]).body) if n not in PROCESS_LEVEL_NAMES]
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build_tool_version": BUILD_TOOL_VERSION,
        "notebook_file": notebook_path.name,
        "notebook_sha256": notebook_sha,
        "core_sha256": sha256_text(generated),
        "code_cell_count": len(cells),
        "stage_functions": stage_functions,
        "per_run_state_names": run_state,
        "config_defaults": config_defaults(config_code),
        "transformations": [
            {"cell": "e7602a7f", "change": "CFG = StructuredConfig() → StructuredConfig(**_APP_ENGINE_OVERRIDES)"},
            {"cell": "fedf7d9f", "change": "IPython.display.display : repli print si IPython absent"},
            {"cell": "a98d9d6a", "change": "instructions finales → _nb_stage_inventory()"},
            {"cell": "4869d7f3", "change": "cellule entière → _nb_stage_pass_a()"},
            {"cell": "1687acc2", "change": "cellule entière → _nb_stage_pass_b()"},
            {"cell": "5eb70c68", "change": "cellule entière → _nb_stage_matching_and_reports()"},
        ],
        "cells": manifest_cells,
    }
    return generated, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--notebook", type=Path, default=DEFAULT_NOTEBOOK)
    parser.add_argument("--check", action="store_true", help="vérifier sans écrire")
    args = parser.parse_args(argv)
    try:
        generated, manifest = build(args.notebook.resolve())
    except (BuildError, OSError, json.JSONDecodeError, SyntaxError) as exc:
        print("ÉCHEC GÉNÉRATION MOTEUR :", exc, file=sys.stderr)
        return 2
    if args.check:
        if not CORE_PATH.is_file() or CORE_PATH.read_text(encoding="utf-8") != generated:
            print("ÉCHEC : engine/notebook_core.py ne correspond pas au notebook. Exécuter scripts/build_engine.sh.", file=sys.stderr)
            return 1
        saved = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.is_file() else {}
        if saved.get("notebook_sha256") != manifest["notebook_sha256"] or saved.get("core_sha256") != manifest["core_sha256"]:
            print("ÉCHEC : engine_manifest.json obsolète. Exécuter scripts/build_engine.sh.", file=sys.stderr)
            return 1
        print("OK : moteur conforme au notebook", manifest["notebook_sha256"])
        return 0
    CORE_PATH.write_text(generated, encoding="utf-8")
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Moteur généré : {CORE_PATH} ({len(generated.splitlines())} lignes, {manifest['code_cell_count']} cellules)")
    print("Fonctions d'étape :", ", ".join(manifest["stage_functions"]))
    print("SHA-256 notebook :", manifest["notebook_sha256"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
