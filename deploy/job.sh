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
  logs)  # console logs for the job (Log Analytics; ${2:-60} minutes back)
         WS=$(az rest --method get --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.OperationalInsights/workspaces/log-aziz-trader?api-version=2023-09-01" --query properties.customerId -o tsv)
         az monitor log-analytics query -w "$WS" --analytics-query            "ContainerAppConsoleLogs_CL | where TimeGenerated > ago(${2:-60}m) | project TimeGenerated, Log_s | order by TimeGenerated asc | take 200"            -o table ;;
  *) echo "usage: job.sh start|list|stop <execution>|logs [minutes]"; exit 1 ;;
esac
