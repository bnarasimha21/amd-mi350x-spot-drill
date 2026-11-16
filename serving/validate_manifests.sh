#!/usr/bin/env bash
# Offline validation of serving/k8s. No cluster and no cloud account needed.
#
#   1. kustomize renders (kubectl kustomize, or the kustomize binary)
#   2. kubeconform checks every object against the Kubernetes and KEDA schemas, strictly
#   3. a few checks a schema cannot make: the termination budget adds up, the autoscaling
#      threshold was filled in, nothing asks for a public load balancer
#
# This proves the manifests are well-formed. It does not prove vLLM starts on an MI350X.
#
# Usage: serving/validate_manifests.sh        (K8S_SCHEMA_VERSION=1.36.0 to pick a schema)
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="$(mktemp)"; trap 'rm -f "$OUT" "$OUT.summary"' EXIT

if command -v kubectl >/dev/null; then kubectl kustomize serving/k8s > "$OUT"
elif command -v kustomize >/dev/null; then kustomize build serving/k8s > "$OUT"
else echo "FATAL: need kubectl or kustomize to render serving/k8s"; exit 1; fi
echo "rendered $(grep -c '^kind:' "$OUT") objects"

command -v kubeconform >/dev/null || {
  echo "FATAL: kubeconform not found. Install: go install github.com/yannh/kubeconform/cmd/kubeconform@v0.8.0"; exit 1; }
kubeconform -strict -summary -kubernetes-version "${K8S_SCHEMA_VERSION:-1.36.0}" \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  - < "$OUT" | tee "$OUT.summary"
# kubeconform exits 0 when it is handed nothing, so check it saw every object.
grep -q "Valid: $(grep -c '^kind:' "$OUT"), Invalid: 0, Errors: 0, Skipped: 0" "$OUT.summary" \
  || { echo "FAIL: kubeconform did not validate every rendered object"; exit 1; }

num () { grep -E "$1" "$OUT" | head -1 | grep -oE '[0-9]+' | head -1; }
GRACE=$(num 'terminationGracePeriodSeconds: 1[0-9]+')
PRESTOP=$(grep -A2 'preStop:' "$OUT" | grep -oE 'seconds: [0-9]+' | grep -oE '[0-9]+')
SHUTDOWN=$(num 'SHUTDOWN_TIMEOUT_S: ')
THRESH=$(num 'threshold: ')
MAXREP=$(num 'maxReplicaCount: ')
echo "termination: preStop ${PRESTOP}s + shutdown timeout ${SHUTDOWN}s <= grace ${GRACE}s ?"
[ $((PRESTOP + SHUTDOWN)) -lt "$GRACE" ] || { echo "FAIL: SIGKILL would arrive before vLLM finishes draining"; exit 1; }
[ "$THRESH" -gt 0 ] || { echo "FAIL: ScaledObject threshold is 0; TARGET_INFLIGHT was not copied in"; exit 1; }
echo "autoscaling: threshold $THRESH in-flight per replica, at most $MAXREP replicas"
grep -q -- '--shutdown-timeout=' "$OUT" || { echo "FAIL: vLLM would abort in-flight requests on SIGTERM"; exit 1; }
if grep -qE 'type: (LoadBalancer|NodePort)' "$OUT"; then
  echo "FAIL: a Service is exposed outside the cluster; vLLM here has no API key"; exit 1
fi
grep -q 'maxUnavailable: 1' "$OUT" || { echo "FAIL: PodDisruptionBudget missing"; exit 1; }
echo "MANIFESTS OK (well-formed; not run on a GPU)"
