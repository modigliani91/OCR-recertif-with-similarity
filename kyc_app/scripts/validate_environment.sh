#!/usr/bin/env bash
# Validation de l'environnement Domino (sans charger le modèle 27B).
#   bash scripts/validate_environment.sh            # complet (workspace/app sur tier GPU)
#   bash scripts/validate_environment.sh --no-gpu   # contrôles hors GPU uniquement
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"
PYTHON_BIN="${PYTHON_BIN:-python}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
echo "== Validation environnement KYC ($(date -u +%Y-%m-%dT%H:%M:%SZ)) =="
exec "${PYTHON_BIN}" tools/validate_environment.py "$@"
