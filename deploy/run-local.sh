#!/usr/bin/env bash
# Convenience wrapper for this dev machine: loads the secrets from the Windows User environment
# into this process only (never printed, never stored), then runs the deployment.
#
#   bash deploy/run-local.sh              # rebuild the image and update the job
#   SKIP_BUILD=1 bash deploy/run-local.sh # reuse the current image (e.g. only a secret changed)
#
# Kept in the repo on purpose: a copy living in a temp/scratch directory silently disappeared
# between sessions on 2026-09-22 and a "redeploy" did nothing but print a file-not-found.
set -uo pipefail
cd "$(dirname "$0")/.."
getv() { powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('$1','User')" | tr -d '\r'; }
export UW_API_KEY="$(getv UW_API_KEY)"
export APCA_API_KEY_ID="$(getv APCA_API_KEY_ID)"
export APCA_API_SECRET_KEY="$(getv APCA_API_SECRET_KEY)"
export JOURNAL_PAGE_PASSPHRASE="$(getv JOURNAL_PAGE_PASSPHRASE)"
export REQUESTS_CA_BUNDLE="$PWD/ca_bundle.pem" SSL_CERT_FILE="$PWD/ca_bundle.pem"
export YES=1
bash deploy/azure.sh "${SUBSCRIPTION:-Azure subscription 1}"
echo "DEPLOY EXIT CODE: $?"
