#!/usr/bin/env bash
# Destroy the serving cluster and everything DOKS created for it, then CHECK that nothing
# is left billing. Copy results out of the cluster first: this does not.
#
# Usage: serving/cluster/03_destroy_cluster.sh --confirm
set -euo pipefail
source "$(dirname "$0")/common.sh"
[ "${1:-}" = "--confirm" ] || { echo "Refusing to delete cloud resources without --confirm."; exit 1; }

if ! cluster_exists; then
  echo "No cluster named '$SERVING_CLUSTER'. Checking for leftovers anyway."
else
  CID=$(doctl kubernetes cluster get "$SERVING_CLUSTER" --format ID --no-header)
  echo "$CID" > "$STATE_DIR/serving-cluster-id" 2>/dev/null || true
  echo "About to destroy cluster $SERVING_CLUSTER ($CID) and its volumes and load balancers:"
  doctl kubernetes cluster node-pool list "$SERVING_CLUSTER" --format Name,Size,Count --no-header || true
  echo "Proceeding in 5s (ctrl-c to abort)..."; sleep "${SERVING_CONFIRM_SLEEP:-5}"
  # --dangerous also deletes volumes, snapshots and load balancers the cluster created.
  doctl kubernetes cluster delete "$SERVING_CLUSTER" --dangerous --force
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/serving-destroyed-at" 2>/dev/null || true
fi

CID="${CID:-$(cat "$STATE_DIR/serving-cluster-id" 2>/dev/null || true)}"
echo "Checking for leftovers (max 5 min)..."
LEFT=""
for i in $(seq 1 60); do
  LEFT=""
  cluster_exists && LEFT="cluster"
  if [ -n "$CID" ]; then
    D=$(doctl compute droplet list --tag-name "k8s:$CID" --format ID,Name,Status --no-header 2>/dev/null || true)
    [ -n "$D" ] && LEFT="$LEFT droplets"
  fi
  [ -z "$LEFT" ] && break
  sleep 5
done
if [ -n "$LEFT" ]; then
  echo "!!! STILL PRESENT after 5 min:$LEFT. SOMETHING MAY STILL BE BILLING. Check by hand NOW:"
  echo "    doctl kubernetes cluster list"
  [ -n "$CID" ] && echo "    doctl compute droplet list --tag-name k8s:$CID"
  exit 1
fi
echo "Cluster and its Droplets are gone. Billing for them has stopped."
echo "Still check by hand: doctl compute volume list ; doctl compute load-balancer list"
if [ -f "$STATE_DIR/serving-started-at" ]; then
  echo "Session: started $(cat "$STATE_DIR/serving-started-at"), destroyed $(cat "$STATE_DIR/serving-destroyed-at" 2>/dev/null || echo '?')."
  echo "The invoice is the record of what it cost, not this script."
fi
