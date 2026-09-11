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
# names derived from the subscription id so re-running the script updates the same resources
az account set --subscription "$SUB"
SUFFIX=$(az account show --query id -o tsv | tr -d '-' | cut -c1-10)
STORAGE="${STORAGE:-stazizj$SUFFIX}"              # globally unique, lowercase, <=24 chars
ACR="${ACR:-acrazizt$SUFFIX}"
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
if [[ "${YES:-}" != "1" ]]; then read -r -p "Continue? [y/N] " ok; [[ "$ok" == "y" ]] || exit 1; fi

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

echo "== container apps environment + job (ARM template; no CLI extension needed)"
az provider register -n Microsoft.App --wait -o none
az provider register -n Microsoft.OperationalInsights --wait -o none
az deployment group create -g "$RG" -n "paper-trader-$(date +%Y%m%d%H%M%S)"    --template-file "$(dirname "$0")/job.json"    --parameters location="$LOCATION" envName="$ENV_NAME" jobName="$JOB" image="$ACR_SERVER/paper-trader:latest"                 registryServer="$ACR_SERVER" registryUsername="$ACR_USER" registryPassword="$ACR_PASS"                 cron="$CRON" traderArgs="${TRADER_ARGS:-}"                 uwKey="$UW_API_KEY" apcaKey="$APCA_API_KEY_ID" apcaSecret="$APCA_API_SECRET_KEY"                 sasUrl="$SAS_URL" pagePass="$JOURNAL_PAGE_PASSPHRASE"    --query "properties.provisioningState" -o tsv

printf 'JOURNAL_BLOB_SAS_URL=%s
DATA_URL=%s
' "$SAS_URL" "$DATA_URL" > "$(dirname "$0")/secrets.local.env"
cat <<EOF

DONE.
  Job:        $JOB in $RG  (cron '$CRON' UTC, one execution per weekday, exits after 15:45 ET)
  Run now:    bash deploy/job.sh start
  Executions: bash deploy/job.sh list
  Logs:       Azure portal -> Container Apps Jobs -> $JOB -> Execution history -> Console logs
              (the 'az containerapp' extension cannot install on this machine's CLI; job.sh uses the REST API)
  Journal:    write-access SAS URL saved to deploy/secrets.local.env (git-ignored) -- keep it out of the site
  Page data:  $DATA_URL
              -> paste into site/journal.html as DATA_URL, commit journal.html to the svrtechservices repo
EOF
