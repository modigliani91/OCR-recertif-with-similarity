#!/usr/bin/env bash
# Point d'entrée exigé par Domino (Publish → App) : délègue au script de lancement versionné.
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/run_app.sh" "$@"
