#!/usr/bin/env bash
# Nettoyage prudent des fichiers temporaires (simulation par défaut ; rien n'est supprimé sans --apply).
#   bash scripts/cleanup_runtime.sh                              # liste ce qui serait supprimé
#   bash scripts/cleanup_runtime.sh --apply                      # supprime uploads interrompus, anciens statuts
#   bash scripts/cleanup_runtime.sh --apply --include-old-extractions
#   bash scripts/cleanup_runtime.sh --lock-status                # état du verrou de traitement
#   bash scripts/cleanup_runtime.sh --break-stale-lock           # quarantaine d'un verrou ORPHELIN uniquement
# Ne supprime jamais : checkpoints, sorties validées (JSON/CSV/Excel), journaux, référence, modèle.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"
PYTHON_BIN="${PYTHON_BIN:-python}"
case "${1:-}" in
  --lock-status) exec "${PYTHON_BIN}" -m run_lock --status ;;
  --break-stale-lock) exec "${PYTHON_BIN}" -m run_lock --break ;;
  *) exec "${PYTHON_BIN}" tools/cleanup_runtime.py "$@" ;;
esac
