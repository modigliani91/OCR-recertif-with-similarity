#!/usr/bin/env bash
# (Re)génère engine/notebook_core.py et engine/engine_manifest.json depuis le notebook de référence.
# À exécuter après toute modification validée du notebook, puis redémarrer l'application.
#   bash scripts/build_engine.sh            # générer
#   bash scripts/build_engine.sh --check    # vérifier sans écrire
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"
exec "${PYTHON_BIN:-python}" engine/build_engine.py "$@"
