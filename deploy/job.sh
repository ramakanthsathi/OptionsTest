#!/usr/bin/env bash
# Start / list executions of the Container Apps Job via the REST API (no CLI extension required).
#   bash deploy/job.sh start | list | stop <executionName>
set -euo pipefail
RG="${RG:-rg-aziz-trader}"; JOB="${JOB:-job-paper-trader}"
SUB=$(az account show --query id -o tsv)
BASE="https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.App/jobs/$JOB"
API="api-version=2024-03-01"
case "${1:-list}" in
  start) az rest --method post --url "$BASE/start?$API" --query "{execution:name, id:id}" -o json ;;
  list)  az rest --method get  --url "$BASE/executions?$API" --query "value[].{name:name, status:properties.status, start:properties.startTime, end:properties.endTime}" -o table ;;
  stop)  az rest --method post --url "$BASE/executions/${2:?execution name}/stop?$API" ;;
  *) echo "usage: job.sh start|list|stop <execution>"; exit 1 ;;
esac
