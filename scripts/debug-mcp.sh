#!/bin/bash
# Diagnostic dump for a stuck/failed SURIMI data server (surimi-data-mcp) helm release.
#
# Usage:
#   ./debug-mcp.sh                        # auto-detect: latest surimi-data-mcp release
#   ./debug-mcp.sh <release-name>         # specific release
#
# Run it from a pod that has kubectl, helm and the namespace's service account, such as the surimi-terminal pod.

set -uo pipefail

REL="${1:-}"
if [ -z "$REL" ]; then
  REL=$(helm list -q 2>/dev/null | grep -E '^surimi-(data-)?mcp' | tail -1 || true)
fi
if [ -z "$REL" ]; then
  echo "ERROR: no surimi-data-mcp helm release found. Pass a name explicitly."
  echo "  ./debug-mcp.sh <release-name>"
  echo
  echo "Available releases:"
  helm list -a 2>/dev/null
  exit 1
fi

sep() { printf '\n=== %s ===\n' "$*"; }

sep "release: $REL"
helm status "$REL" 2>&1 | head -30

sep "pods (any state)"
kubectl get pod -l app.kubernetes.io/instance="$REL" -o wide 2>&1

sep "describe each pod"
for p in $(kubectl get pod -l app.kubernetes.io/instance="$REL" -o name 2>/dev/null); do
  echo "--- $p ---"
  kubectl describe "$p" 2>&1 | tail -40
  echo
done

sep "mcp server logs (last 80 lines)"
# The release label matches every pod of the release, so this picks the server pod by its name.
SRV_POD=$(kubectl get pods -o name 2>/dev/null | grep -E "^pod/${REL}-[0-9a-f]{8,}" | head -1)
if [ -n "$SRV_POD" ]; then
  kubectl logs "$SRV_POD" -c surimi-data-mcp --tail=80 2>&1 || kubectl logs "$SRV_POD" --tail=80 2>&1
else
  echo "(no server pod found)"
fi

# The job is named <release>-load-data-r<revision>; take the newest one.
JOB=$(kubectl get jobs -o name 2>/dev/null | sed 's#^job.batch/##' | grep "^${REL}-load-data" | sort -V | tail -1)
sep "load-data Job status"
if [ -n "$JOB" ]; then kubectl get job "$JOB" 2>&1; else echo "(no load-data job - normal if postgresql.enabled=false)"; fi

sep "load-data Job logs (last 150 lines)"
JOB_POD=$( [ -n "$JOB" ] && kubectl get pod -l job-name="$JOB" -o name 2>/dev/null | head -1 )
if [ -n "$JOB_POD" ]; then
  kubectl logs "$JOB_POD" -c load-csv --tail=150 2>&1 || true
  echo "--- previous attempt (if any) ---"
  kubectl logs "$JOB_POD" -c load-csv --previous --tail=80 2>&1 || true
else
  echo "(no load-data pod found)"
fi

sep "postgres pod logs (last 30 lines)"
kubectl logs "${REL}-postgresql-0" --tail=30 2>&1 || echo "(no bundled postgres or different name)"

sep "namespace events (most recent 25)"
kubectl get events --sort-by=.lastTimestamp 2>&1 | tail -25

sep "ingress + service"
kubectl get svc,ingress -l app.kubernetes.io/instance="$REL" 2>&1

sep "done"
echo "If you found the error, common fixes:"
echo "  - ImagePullBackOff      => image tag wrong or registry rate-limit; check helm get values \$REL"
echo "  - Pending (quota)       => initContainers missing requests.cpu/memory; check describe pod"
echo "  - load-csv connection   => postgres not ready yet; usually self-heals on Job retry"
echo "  - InvalidAccessKeyId    => the S3 STS token expired; relaunch the service on EDITO for a fresh one"
echo "  - load-csv missing CSVs => SURIMI_DATA_SRC prefix empty or wrong, or the S3 token expired; check the load-data Job env and MinIO"
echo "  - SSE 200 but tools 0   => the server started but could not reach the database; check env DATABASE_URL"
