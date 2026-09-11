#!/usr/bin/env bash
# One-time Azure deployment of the paper trader as a scheduled Container Apps Job.
#
#   1. az login                      (YOUR PERSONAL subscription -- this script refuses employer tenants)
#   2. export UW_API_KEY=...  APCA_API_KEY_ID=...  APCA_API_SECRET_KEY=...  JOURNAL_PAGE_PASSPHRASE=...
#   3. bash deploy/azure.sh "<subscription name or id>"
#
# Creates: resource group, storage account (+ 'journal' container, public-read blobs, CORS for the site),
#          container registry (image built in the cloud, no local Docker needed), Container Apps
#          environment, and a cron-triggered Job that runs one trading day per execution.
# Prints the DATA_URL to paste into site/journal.html.  Re-running updates the image + job in place.
#
# Cost (consumption plan, one ~6.5 h execution per weekday, 0.5 vCPU / 1 GiB): on the order of $1-3/month
# plus ~$5/month for the Basic registry and cents for storage. Check your own bill.
set -euo pipefail

SUB="${1:?usage: deploy/azure.sh <subscription name or id>}"
LOCATION="${LOCATION:-eastus}"
RG="${RG:-rg-aziz-trader}"
STORAGE="${STORAGE:-stazizjournal$RANDOM}"        # must be globally unique, lowercase, <=24 chars
ACR="${ACR:-acrazizt$RANDOM}"
ENV_NAME="${ENV_NAME:-cae-aziz-trader}"
JOB="${JOB:-job-paper-trader}"
SITE_ORIGIN="${SITE_ORIGIN:-https://svrtechservices.com}"
# 13:25 UTC = 09:25 EDT (08:25 EST). The container sleeps until 09:40 ET itself (--wait-until), so DST
# is handled in the script, not the cron. Container Apps cron is UTC only.
CRON="${CRON:-25 13 * * 1-5}"

for v in UW_API_KEY APCA_API_KEY_ID APCA_API_SECRET_KEY JOURNAL_PAGE_PASSPHRASE; do
  [[ -n "${!v:-}" ]] || { echo "env var $v is not set (export it in this shell; it is never printed)"; exit 1; }
done

az account set --subscription "$SUB"
SUB_NAME=$(az account show --query name -o tsv)
TENANT_USER=$(az account show --query user.name -o tsv)
echo "Deploying to subscription: $SUB_NAME  (as $TENANT_USER)"
if [[ "$SUB_NAME" =~ [Tt]egna || "$TENANT_USER" =~ tegna ]]; then
  echo "REFUSING: this looks like an employer subscription. Log in to your personal one."; exit 1
fi
read -r -p "Continue? [y/N] " ok; [[ "$ok" == "y" ]] || exit 1

echo "== resource group"; az group create -n "$RG" -l "$LOCATION" -o none

echo "== storage"
az storage account create -n "$STORAGE" -g "$RG" -l "$LOCATION" --sku Standard_LRS --kind StorageV2 \
   --allow-blob-public-access true --min-tls-version TLS1_2 -o none
KEY=$(az storage account keys list -n "$STORAGE" -g "$RG" --query "[0].value" -o tsv)
az storage container create -n journal --account-name "$STORAGE" --account-key "$KEY" --public-access blob -o none
az storage cors clear --services b --account-name "$STORAGE" --account-key "$KEY" -o none
az storage cors add --services b --methods GET HEAD --origins "$SITE_ORIGIN" "https://www.${SITE_ORIGIN#https://}" "http://localhost:8765" \
   --allowed-headers "*" --exposed-headers "*" --max-age 3600 --account-name "$STORAGE" --account-key "$KEY" -o none
EXPIRY=$(date -u -d "+365 days" +%Y-%m-%dT%H:%MZ 2>/dev/null || date -u -v+365d +%Y-%m-%dT%H:%MZ)
SAS=$(az storage container generate-sas -n journal --account-name "$STORAGE" --account-key "$KEY" \
      --permissions racwl --expiry "$EXPIRY" --https-only -o tsv)
SAS_URL="https://${STORAGE}.blob.core.windows.net/journal?${SAS}"
DATA_URL="https://${STORAGE}.blob.core.windows.net/journal/today.json.enc"

echo "== registry + cloud build"
az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled true -o none
az acr build -r "$ACR" -t paper-trader:latest "$(dirname "$0")/.." -o none
ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)
ACR_USER=$(az acr credential show -n "$ACR" --query username -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR" --query "passwords[0].value" -o tsv)

echo "== container apps environment"
az extension add --name containerapp --upgrade -o none 2>/dev/null || true
az provider register -n Microsoft.App --wait -o none
az provider register -n Microsoft.OperationalInsights --wait -o none
az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOCATION" -o none 2>/dev/null || true

echo "== job"
COMMON=(--resource-group "$RG" --image "$ACR_SERVER/paper-trader:latest"
        --registry-server "$ACR_SERVER" --registry-username "$ACR_USER" --registry-password "$ACR_PASS"
        --secrets "uw-key=$UW_API_KEY" "apca-key=$APCA_API_KEY_ID" "apca-secret=$APCA_API_SECRET_KEY"
                  "sas-url=$SAS_URL" "page-pass=$JOURNAL_PAGE_PASSPHRASE"
        --env-vars "UW_API_KEY=secretref:uw-key" "APCA_API_KEY_ID=secretref:apca-key" "APCA_API_SECRET_KEY=secretref:apca-secret"
                   "JOURNAL_BLOB_SAS_URL=secretref:sas-url" "JOURNAL_PAGE_PASSPHRASE=secretref:page-pass"
                   "TRADER_ARGS=${TRADER_ARGS:-}"
        --cpu 0.5 --memory 1.0Gi)
if az containerapp job show -n "$JOB" -g "$RG" -o none 2>/dev/null; then
  az containerapp job update -n "$JOB" "${COMMON[@]}" -o none
else
  az containerapp job create -n "$JOB" --environment "$ENV_NAME" --trigger-type Schedule --cron-expression "$CRON" \
     --replica-timeout 27000 --replica-retry-limit 0 --parallelism 1 --replica-completion-count 1 "${COMMON[@]}" -o none
fi

cat <<EOF

DONE.
  Job:        $JOB in $RG  (cron '$CRON' UTC, one execution per weekday, exits after 15:45 ET)
  Run now:    az containerapp job start -n $JOB -g $RG
  Logs:       az containerapp job execution list -n $JOB -g $RG -o table
              az containerapp logs show -n $JOB -g $RG --type console --follow   (during a run)
  Journal:    $SAS_URL   <-- SECRET (write access); keep it out of the site
  Page data:  $DATA_URL
              -> paste into site/journal.html as DATA_URL, commit journal.html to the svrtechservices repo
EOF
