#!/usr/bin/env bash
# Lancement de l'application KYC dans Domino Data Lab (un seul processus Streamlit).
#
#   bash scripts/run_app.sh               # depuis la racine du projet (ou via app.sh)
#
# Variables utiles : PORT / KYC_APP_PORT (défaut 8888, exigé par Domino), PYTHON_BIN,
# KYC_OUTPUT_ROOT, KYC_APP_ROOT, KYC_REFERENCE_CSV, KYC_MODEL_PATH, KYC_RUN_MODE,
# KYC_MAX_UPLOAD_MB, KYC_STREAMLIT_BASE_URL_PATH (uniquement si votre proxy Domino l'exige).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PORT="${KYC_APP_PORT:-${PORT:-8888}}"
PYTHON_BIN="${PYTHON_BIN:-python}"
export KYC_OUTPUT_ROOT="${KYC_OUTPUT_ROOT:-/mnt/output_qwen38}"
export KYC_APP_ROOT="${KYC_APP_ROOT:-/mnt/kyc_app}"
export KYC_MAX_UPLOAD_MB="${KYC_MAX_UPLOAD_MB:-4096}"
# Inférence 100 % locale : aucun accès au Hub Hugging Face, aucune télémétrie.
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Un seul processus applicatif : pas de fork de tokenizers ni de workers multiples.
export TOKENIZERS_PARALLELISM=false

log() { printf '[run_app] %s\n' "$*"; }
fail() { printf '[run_app] ERREUR : %s\n' "$*" >&2; exit 1; }

command -v "${PYTHON_BIN}" >/dev/null 2>&1 || fail "interpréteur ${PYTHON_BIN} introuvable"
"${PYTHON_BIN}" - <<'PY' || fail "Python 3.10 ou ultérieur requis"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
"${PYTHON_BIN}" -c "import streamlit" 2>/dev/null \
  || fail "Streamlit absent : l'ajouter à l'environnement Domino (voir README, section Environnement)"

for required in app.py config.py pipeline.py job_manager.py engine/notebook_core.py engine/engine_manifest.json; do
  [[ -f "${PROJECT_ROOT}/${required}" ]] || fail "fichier requis absent : ${required}"
done

# Le moteur doit correspondre octet pour octet au notebook de référence (si présent).
if [[ -f "${PROJECT_ROOT}/kyc_qwen38_production_kyc_pipeline_name_similarity_v2.ipynb" ]]; then
  "${PYTHON_BIN}" engine/build_engine.py --check \
    || fail "moteur obsolète : exécuter 'bash scripts/build_engine.sh' puis relancer"
else
  log "ATTENTION : notebook de référence absent du projet ; contrôle de conformité du moteur ignoré"
fi

# Persistance (Domino) : les Apps ne réécrivent pas leurs fichiers locaux dans le projet ; sans volume
# persistant, les checkpoints écrits par l'App sont perdus à son redémarrage. Si KYC_PERSISTENT_OUTPUT_DIR
# désigne un Dataset/EDV (ex. /mnt/data/kyc_outputs/output_qwen38), le chemin métier
# ${KYC_OUTPUT_ROOT} reste inchangé et devient un lien vers ce volume.
if [[ -n "${KYC_PERSISTENT_OUTPUT_DIR:-}" ]]; then
  mkdir -p "${KYC_PERSISTENT_OUTPUT_DIR}" || fail "volume persistant inaccessible : ${KYC_PERSISTENT_OUTPUT_DIR}"
  if [[ -L "${KYC_OUTPUT_ROOT}" || ! -e "${KYC_OUTPUT_ROOT}" ]]; then
    ln -sfn "${KYC_PERSISTENT_OUTPUT_DIR}" "${KYC_OUTPUT_ROOT}"
    log "Sorties      : ${KYC_OUTPUT_ROOT} → ${KYC_PERSISTENT_OUTPUT_DIR} (persistant)"
  else
    log "ATTENTION : ${KYC_OUTPUT_ROOT} existe déjà comme dossier réel ; lien vers ${KYC_PERSISTENT_OUTPUT_DIR} NON créé"
  fi
else
  log "ATTENTION : KYC_PERSISTENT_OUTPUT_DIR non défini — checkpoints conservés le temps de vie de l'App uniquement"
fi

mkdir -p "${KYC_OUTPUT_ROOT}/logs" "${KYC_OUTPUT_ROOT}/reports" "${KYC_OUTPUT_ROOT}/app_runs" \
         "${KYC_APP_ROOT}/input" "${KYC_APP_ROOT}/extracted" \
  || fail "impossible de créer les dossiers sous /mnt (droits ?)"

log "Projet       : ${PROJECT_ROOT}"
log "Python       : $("${PYTHON_BIN}" -c 'import sys; print(sys.version.split()[0])')"
log "Streamlit    : $("${PYTHON_BIN}" -c 'import streamlit; print(streamlit.__version__)')"
log "Adresse/port : 0.0.0.0:${PORT}"
"${PYTHON_BIN}" - <<'PY' || fail "configuration invalide (voir message ci-dessus)"
import json
from config import load_config, pipeline_versions
print(json.dumps({**load_config().describe(), **{k: v for k, v in pipeline_versions().items() if k != "NOTEBOOK_SHA256"}},
                 ensure_ascii=False, indent=2))
PY

EXTRA_ARGS=()
if [[ -n "${KYC_STREAMLIT_BASE_URL_PATH:-}" ]]; then
  EXTRA_ARGS+=("--server.baseUrlPath=${KYC_STREAMLIT_BASE_URL_PATH}")
  log "baseUrlPath  : ${KYC_STREAMLIT_BASE_URL_PATH}"
fi

log "Démarrage de Streamlit (processus unique, modèle chargé au premier traitement)…"
exec "${PYTHON_BIN}" -m streamlit run app.py \
  --server.address=0.0.0.0 \
  --server.port="${PORT}" \
  --server.headless=true \
  --server.fileWatcherType=none \
  --server.runOnSave=false \
  --server.enableCORS=false \
  --server.enableXsrfProtection=false \
  --server.maxUploadSize="${KYC_MAX_UPLOAD_MB}" \
  --browser.gatherUsageStats=false \
  --browser.serverAddress=localhost \
  --client.showErrorDetails=false \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
