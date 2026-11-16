#!/usr/bin/env bash
# Install KEDA, then the manifests in serving/k8s, and MEASURE the first vLLM cold start:
# apply -> pod scheduled -> pod Ready (image pull + model download + model load).
#
# Usage: serving/cluster/02_install.sh
set -euo pipefail
source "$(dirname "$0")/common.sh"
mkdir -p "$STATE_DIR"

CTX=$(kubectl config current-context)
case "$CTX" in
  *"$SERVING_CLUSTER"*) ;;
  *) echo "REFUSING: kubectl context is '$CTX', not the serving cluster '$SERVING_CLUSTER'."
     echo "Fix: doctl kubernetes cluster kubeconfig save $SERVING_CLUSTER"; exit 1 ;;
esac

echo "=== KEDA $KEDA_VERSION ==="
kubectl apply --server-side -f "https://github.com/kedacore/keda/releases/download/v${KEDA_VERSION}/keda-${KEDA_VERSION}.yaml"
# Deployment names as they are in the release manifest (the Helm chart names differ).
# keda-admission must be up before a ScaledObject is applied: it validates them.
for d in keda-operator keda-metrics-apiserver keda-admission; do
  kubectl -n keda rollout status "deployment/$d" --timeout=300s
done

echo "=== manifests ==="
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create configmap loadgen-script --from-file=serving/loadgen.py \
  --dry-run=client -o yaml | kubectl apply -f -
T_APPLY=$(date +%s)
kubectl apply -k serving/k8s
kubectl -n "$NS" rollout status deployment/prometheus --timeout=300s
kubectl -n "$NS" rollout status deployment/loadgen --timeout=300s

echo "=== waiting for the first vLLM pod (max 35 min; the startup probe allows 30) ==="
T_SCHED=""
for i in $(seq 1 420); do
  if [ -z "$T_SCHED" ] && kubectl -n "$NS" get pods -l app=vllm \
       -o jsonpath='{.items[*].spec.nodeName}' 2>/dev/null | grep -q .; then
    T_SCHED=$(date +%s)
  fi
  READY=$(kubectl -n "$NS" get deployment vllm -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
  [ "${READY:-0}" -ge 1 ] 2>/dev/null && break
  [ "$i" = 420 ] && {
    echo "vLLM never became Ready. The cluster IS BILLING. Look, then fix or destroy:"
    echo "  kubectl -n $NS describe pod -l app=vllm | tail -40"
    echo "  kubectl -n $NS logs -l app=vllm --tail=80"
    echo "  serving/cluster/03_destroy_cluster.sh --confirm"; exit 1; }
  sleep 5
done
T_READY=$(date +%s)
echo "== COLD START: apply->scheduled $(( ${T_SCHED:-$T_READY} - T_APPLY ))s, apply->ready $((T_READY - T_APPLY))s =="
echo "{\"event\":\"vllm_first_ready\",\"apply_to_scheduled_s\":$(( ${T_SCHED:-$T_READY} - T_APPLY )),\"apply_to_ready_s\":$((T_READY - T_APPLY))}" \
  | tee -a "$STATE_DIR/serving-events.jsonl"

echo "=== smoke test from inside the cluster ==="
kubectl -n "$NS" exec deploy/loadgen -- python3 -c '
import json, urllib.request
print("models:", [m["id"] for m in json.load(urllib.request.urlopen("http://vllm:8000/v1/models", timeout=10))["data"]])'
kubectl -n "$NS" get scaledobject,hpa,pdb,pods -o wide
echo "Installed. The cluster is BILLING. Continue with SERVING.md phase 4."
