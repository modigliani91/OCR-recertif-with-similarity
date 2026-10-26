#!/usr/bin/env bash
# Tests applicatifs (aucun appel GPU). Le test d'intégration « dry-run » utilise le vrai moteur
# du notebook avec un Qwen simulé ; il est ignoré si PyTorch/Transformers sont absents.
#   bash scripts/run_tests.sh
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
exec "${PYTHON_BIN:-python}" -m pytest tests -q "$@"
