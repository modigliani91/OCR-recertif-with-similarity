"""Nettoyage prudent des fichiers TEMPORAIRES de l'application (simulation par défaut).

Supprimable (avec --apply uniquement) :
  * fichiers d'upload interrompus ``<input>/.upload-*.part`` de plus d'une heure ;
  * répertoires d'extraction d'archives ANCIENNES, non référencés par l'inventaire courant
    (``inventory/population.json``), et dossiers de travail ``kyc_extract_*`` abandonnés ;
  * dossiers ``app_runs/<run_id>`` (statut/progression) plus anciens que --keep-days, sauf le dernier ;
  * verrous mis en quarantaine ``.kyc_processing.lock.stale-*`` plus anciens que --keep-days.

JAMAIS supprimé : checkpoints, raw_ocr, structured, customers, inventory, global, reports,
logs, images, fichier de référence, modèle, ZIP d'entrée courant.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

EXTRACTION_DIR_PATTERN = re.compile(r"^[0-9a-f]{20}_[0-9a-f]{10}$")


def referenced_extractions(output_root: Path) -> set[str]:
    population = output_root / "inventory" / "population.json"
    try:
        data = json.loads(population.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    names = set()
    for selection in data.get("selections", []):
        path = selection.get("full_path")
        if path:
            names.update(part for part in Path(path).parts if EXTRACTION_DIR_PATTERN.match(part))
    return names


def main() -> int:
    from config import load_config

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="supprimer réellement (sinon simulation)")
    parser.add_argument("--keep-days", type=int, default=30)
    parser.add_argument("--include-old-extractions", action="store_true",
                        help="supprimer aussi les extractions d'anciennes archives (recréées si besoin)")
    args = parser.parse_args()
    cfg = load_config()
    now = time.time()
    candidates: list[Path] = []
    for part in cfg.input_dir.glob(".upload-*.part"):
        if now - part.stat().st_mtime > 3600:
            candidates.append(part)
    if cfg.extract_dir.is_dir():
        keep = referenced_extractions(cfg.output_root)
        for child in cfg.extract_dir.iterdir():
            if child.is_dir() and child.name.startswith("kyc_extract_") and now - child.stat().st_mtime > 3600:
                candidates.append(child)
            elif (args.include_old_extractions and child.is_dir() and EXTRACTION_DIR_PATTERN.match(child.name)
                  and child.name not in keep and keep):
                candidates.append(child)
    last = None
    try:
        last = json.loads((cfg.runs_dir / "last_run.json").read_text(encoding="utf-8")).get("run_id")
    except (OSError, ValueError):
        pass
    if cfg.runs_dir.is_dir():
        for child in cfg.runs_dir.iterdir():
            if child.is_dir() and child.name != last and now - child.stat().st_mtime > args.keep_days * 86400:
                candidates.append(child)
    for stale in cfg.output_root.glob(".kyc_processing.lock.stale-*"):
        if now - stale.stat().st_mtime > args.keep_days * 86400:
            candidates.append(stale)
    protected = {cfg.output_root / name for name in ("checkpoints", "raw_ocr", "structured", "customers", "inventory",
                                                     "global", "reports", "logs", "images", "performance")}
    for path in candidates:
        if any(path == p or p in path.parents for p in protected) or path == cfg.reference_csv:
            print("REFUS (protégé) :", path)
            continue
        print(("SUPPRESSION : " if args.apply else "à supprimer (simulation) : ") + str(path))
        if args.apply:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    if not candidates:
        print("Rien à nettoyer.")
    elif not args.apply:
        print("Simulation uniquement : relancer avec --apply pour supprimer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
