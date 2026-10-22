#!/usr/bin/env bash
# Usage: bash scripts/queue_loadtest.sh prod enqueue demo-001 10000
#        bash scripts/queue_loadtest.sh prod status demo-001
#        bash scripts/queue_loadtest.sh prod cancel demo-001
set -euo pipefail
environment=${1:?Expected dev or prod}
mode=${2:?Expected enqueue, status or cancel}
batch=${3:?Expected a unique batch name}
count=${4:-10000}
case "$environment" in dev|prod) ;; *) echo 'Expected dev or prod' >&2; exit 2 ;; esac
case "$mode" in enqueue|status|cancel) ;; *) echo 'Invalid mode' >&2; exit 2 ;; esac
[[ "$batch" =~ ^[A-Za-z0-9-]{1,64}$ ]] || { echo 'Invalid batch name' >&2; exit 2; }
[[ "$count" =~ ^[1-9][0-9]{0,4}$ ]] && ((count <= 50000)) || { echo 'Count must be 1-50000' >&2; exit 2; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Reuse the existing cron container; no deployment, restart or new pod is needed.
# The image entrypoint supplies the Secret-derived DB arguments to Odoo shell.
kubectl -n "odoo-$environment" exec -i deployment/odoo-cron -c odoo-cron -- \
  env ODOO_QUEUE_JOB_CHANNELS=root:0 LOADTEST_MODE="$mode" \
  LOADTEST_BATCH="$batch" LOADTEST_COUNT="$count" LOADTEST_DATABASE="odoo17_$environment" \
  /entrypoint.sh odoo shell -c /etc/odoo/odoo.conf -d "odoo17_$environment" \
  --no-http --workers=0 --max-cron-threads=0 < "$script_dir/queue_loadtest.py"
