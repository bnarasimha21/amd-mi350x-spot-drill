#!/usr/bin/env bash
# Create the serving cluster: one small CPU pool and one autoscaled Spot GPU pool.
# MEASURES: create -> control plane up -> first GPU node Ready -> GPU allocatable.
#
# Why the GPU pool is created with its autoscaling range up front: DOKS does not allow a
# Spot GPU pool's node count or autoscaling maximum to be RAISED after creation. The range
# given here is the range for the life of the pool. The Spot rate is locked now too.
#
# Usage: serving/cluster/01_create_cluster.sh --confirm
set -euo pipefail
source "$(dirname "$0")/common.sh"
[ "${1:-}" = "--confirm" ] || {
  echo "Refusing to create billable cloud resources without --confirm."
  echo "Run serving/cluster/00_preflight.sh first; it creates nothing."
  exit 1
}
: "${SERVING_REGION:?set SERVING_REGION (or REGION) in .env}"
: "${SERVING_GPU_SIZE:?set SERVING_GPU_SIZE (or SPOT_SIZE_SLUG) in .env}"
: "${SERVING_CPU_SIZE:?set SERVING_CPU_SIZE in .env (00_preflight.sh lists what DOKS offers)}"
mkdir -p "$STATE_DIR"

if cluster_exists; then
  echo "A cluster named '$SERVING_CLUSTER' already exists. Destroy it or pick another name."; exit 1
fi

trap 'echo "!! FAILED. If the cluster was created it IS BILLING. Check:";
      echo "   doctl kubernetes cluster list";
      echo "   serving/cluster/03_destroy_cluster.sh --confirm"' ERR

GPU_PRICE=$(size_price "$SERVING_GPU_SIZE")
CPU_PRICE=$(size_price "$SERVING_CPU_SIZE")
echo "!!! COST: up to $SERVING_GPU_MAX x $SERVING_GPU_SIZE at ~\$${GPU_PRICE:-?}/hr each (today's posted price),"
echo "!!!       plus $SERVING_CPU_COUNT x $SERVING_CPU_SIZE at ~\$${CPU_PRICE:-?}/hr. Per-second billing"
echo "!!!       from the moment a node is ready. Budget: \$$SERVING_BUDGET_USD. Set a timer for the hard stop."
echo "Creating $SERVING_CLUSTER in $SERVING_REGION in 10s (ctrl-c to abort)..."
sleep "${SERVING_CONFIRM_SLEEP:-10}"

# The rate is locked for the life of the pool. Keep it: the cost calculation needs this
# number, not whatever the catalogue says later.
echo "${GPU_PRICE:-}" > "$STATE_DIR/serving-gpu-price-hourly"
echo "${CPU_PRICE:-}" > "$STATE_DIR/serving-cpu-price-hourly"
date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/serving-started-at"
T_CREATE=$(date +%s)

# --ha=false: a highly available control plane is billed and is the default on new
# versions; a drill does not need it.
# --enable-amd-gpu-device-plugin: exposes the GPU as amd.com/gpu. Not on by default from
# DOKS 1.37.
doctl kubernetes cluster create "$SERVING_CLUSTER" \
  --region "$SERVING_REGION" --version "$SERVING_K8S_VERSION" \
  --ha=false --enable-amd-gpu-device-plugin=true \
  --tag "$SERVING_TAG" \
  --node-pool "name=cpu;size=$SERVING_CPU_SIZE;count=$SERVING_CPU_COUNT" \
  --node-pool "name=gpu-spot;size=$SERVING_GPU_SIZE;count=$SERVING_GPU_MIN;auto-scale=true;min-nodes=$SERVING_GPU_MIN;max-nodes=$SERVING_GPU_MAX" \
  --update-kubeconfig --set-current-context --wait
T_UP=$(date +%s)
echo "cluster reported running after $((T_UP - T_CREATE))s"

GPU_SEL='doks.digitalocean.com/gpu-brand=amd'
echo "Waiting for a Ready GPU node (max 20 min)..."
for i in $(seq 1 240); do
  kubectl get nodes -l "$GPU_SEL" --no-headers 2>/dev/null | awk '$2=="Ready"' | grep -q . && break
  [ "$i" = 240 ] && { echo "No Ready GPU node. The cluster IS BILLING. Destroy: serving/cluster/03_destroy_cluster.sh --confirm"; exit 1; }
  sleep 5
done
T_NODE=$(date +%s)

echo "Waiting for the node to advertise amd.com/gpu (max 10 min)..."
for i in $(seq 1 120); do
  N=$(kubectl get nodes -l "$GPU_SEL" -o jsonpath='{.items[*].status.allocatable.amd\.com/gpu}' 2>/dev/null || true)
  echo "$N" | grep -q '[1-9]' && break
  [ "$i" = 120 ] && {
    echo "GPU node is Ready but advertises no amd.com/gpu. The cluster IS BILLING."
    echo "Inspect: kubectl describe node -l $GPU_SEL ; kubectl get pods -A | grep -i -E 'amd|gpu'"
    echo "Destroy: serving/cluster/03_destroy_cluster.sh --confirm"; exit 1; }
  sleep 5
done
T_GPU=$(date +%s)

echo "== CLUSTER TIMINGS: create->running $((T_UP - T_CREATE))s, create->gpu-node-ready $((T_NODE - T_CREATE))s, create->gpu-allocatable $((T_GPU - T_CREATE))s =="
echo "{\"event\":\"serving_cluster_created\",\"cluster\":\"$SERVING_CLUSTER\",\"region\":\"$SERVING_REGION\",\"gpu_size\":\"$SERVING_GPU_SIZE\",\"gpu_price_hourly\":\"${GPU_PRICE:-}\",\"cpu_size\":\"$SERVING_CPU_SIZE\",\"cpu_price_hourly\":\"${CPU_PRICE:-}\",\"create_to_running_s\":$((T_UP - T_CREATE)),\"create_to_gpu_node_ready_s\":$((T_NODE - T_CREATE)),\"create_to_gpu_allocatable_s\":$((T_GPU - T_CREATE))}" \
  | tee -a "$STATE_DIR/serving-events.jsonl"
kubectl get nodes -L doks.digitalocean.com/gpu-model,doks.digitalocean.com/capacity-type
echo "GATE REMINDER: confirm the GPU node carries doks.digitalocean.com/capacity-type=spot above."
echo "$SERVING_CLUSTER is up and BILLING. Next: serving/cluster/02_install.sh"
