#!/usr/bin/env bash
# Serving preflight. Creates NOTHING and costs nothing. Run it before every paid session.
#
# Prints what DigitalOcean offers right now (Kubernetes versions, whether the region and the
# sizes are available to DOKS, today's prices) and turns the budget into a hard stop time.
# Spot prices move daily and capacity is as-available, so none of this is cached anywhere.
#
# Usage: serving/cluster/00_preflight.sh [planned-minutes]
set -euo pipefail
source "$(dirname "$0")/common.sh"
PLANNED_MIN="${1:-}"
FAIL=0
: "${SERVING_REGION:?set SERVING_REGION (or REGION) in .env}"
: "${SERVING_GPU_SIZE:?set SERVING_GPU_SIZE (or SPOT_SIZE_SLUG) in .env}"

echo "=== tools ==="
for t in doctl kubectl python3; do
  command -v "$t" >/dev/null && echo "ok   $t" || { echo "MISSING $t"; FAIL=1; }
done

echo; echo "=== account ==="
doctl account get --format Status,DropletLimit --no-header || { echo "doctl is not authenticated"; exit 1; }

echo; echo "=== DOKS versions (SERVING_K8S_VERSION=$SERVING_K8S_VERSION) ==="
doctl kubernetes options versions
echo "From DOKS 1.37 the AMD device plugin is not on by default; 01_create_cluster.sh asks for it."

echo; echo "=== region $SERVING_REGION offered for DOKS? ==="
slugs () { python3 -c 'import json, sys; print("\n".join(x["slug"] for x in json.load(sys.stdin)))'; }
if doctl kubernetes options regions -o json | slugs | grep -qx "$SERVING_REGION"; then
  echo "ok   $SERVING_REGION"
else
  echo "NO   $SERVING_REGION is not in 'doctl kubernetes options regions'"; FAIL=1
fi

echo; echo "=== sizes offered for DOKS node pools ==="
K8S_SIZES=$(doctl kubernetes options sizes -o json | slugs)
echo "GPU sizes DOKS lists today:"; echo "$K8S_SIZES" | grep '^gpu-' | sed 's/^/  /' || echo "  (none)"
for s in "$SERVING_GPU_SIZE" "$SERVING_CPU_SIZE"; do
  [ -n "$s" ] || continue
  echo "$K8S_SIZES" | grep -qx "$s" && echo "ok   $s" || { echo "NO   $s is not a DOKS size today"; FAIL=1; }
done
if [ -z "$SERVING_CPU_SIZE" ]; then
  echo "SERVING_CPU_SIZE is not set. Pick a small CPU size that is offered in $SERVING_REGION"
  echo "from the list above (the drill found s-1vcpu-1gb is not offered in the GPU regions)."
  FAIL=1
fi

echo; echo "=== which regions carry $SERVING_GPU_SIZE (Droplet catalogue) ==="
doctl compute size list -o json | python3 -c '
import json, sys
slug = sys.argv[1]
hit = [s for s in json.load(sys.stdin) if s["slug"] == slug]
print(slug, "regions:", hit[0].get("regions") if hit else "NOT IN CATALOGUE")' "$SERVING_GPU_SIZE"
echo "A listed region is not a promise of capacity. Check the Spot GPU capacity page too."

echo; echo "=== today's prices and the budget ==="
GPU_PRICE=$(size_price "$SERVING_GPU_SIZE")
CPU_PRICE=""; [ -n "$SERVING_CPU_SIZE" ] && CPU_PRICE=$(size_price "$SERVING_CPU_SIZE")
echo "GPU node  $SERVING_GPU_SIZE  \$${GPU_PRICE:-?}/hr   (pool: min $SERVING_GPU_MIN, max $SERVING_GPU_MAX)"
echo "CPU node  ${SERVING_CPU_SIZE:-unset}  \$${CPU_PRICE:-?}/hr   (count $SERVING_CPU_COUNT)"
if [ -n "$GPU_PRICE" ] && [ -n "$CPU_PRICE" ]; then
  python3 serving/budget.py --budget "$SERVING_BUDGET_USD" \
    --gpu-price "$GPU_PRICE" --max-gpu-nodes "$SERVING_GPU_MAX" \
    --cpu-price "$CPU_PRICE" --cpu-nodes "$SERVING_CPU_COUNT" \
    ${PLANNED_MIN:+--planned-minutes "$PLANNED_MIN"} || FAIL=1
else
  echo "Cannot compute the budget without both prices."; FAIL=1
fi

echo; echo "=== anything already running? ==="
if cluster_exists; then
  echo "!! cluster $SERVING_CLUSTER ALREADY EXISTS and is billing."
else
  echo "ok   no cluster named $SERVING_CLUSTER"
fi

echo
[ "$FAIL" = 0 ] && echo "PREFLIGHT OK. Nothing was created." \
                || { echo "PREFLIGHT FAILED. Nothing was created. Fix the lines marked NO or MISSING."; exit 1; }
