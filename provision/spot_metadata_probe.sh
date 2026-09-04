#!/usr/bin/env bash
# Does DigitalOcean expose a PROGRAMMATIC spot-reclamation signal, the way AWS does at
# /latest/meta-data/spot/instance-action? Docs and the public API spec both say no, but the
# metadata service returns undocumented keys (dotty_status, ssh_info) on a real droplet, so
# "not in the docs" is not the same as "not there". This asks an actual spot instance.
#
# The answer is a load-bearing claim in the video, so it gets a receipt rather than a citation.
# Output lands in results/spot-metadata-probe.txt.
#
# Usage: spot_metadata_probe.sh [region]
set -euo pipefail
cd "$(dirname "$0")/.." && source .env
export DIGITALOCEAN_ACCESS_TOKEN="$DO_TOKEN"

NAME="spot-meta-probe-$$"
USE_REGION="${1:-$REGION}"
if [ "$USE_REGION" = "$FALLBACK_REGION" ]; then USE_SIZE="$FALLBACK_SIZE_SLUG"; else USE_SIZE="$SPOT_SIZE_SLUG"; fi
OUT="results/spot-metadata-probe.txt"
mkdir -p results
ID=""

# A $4/hr box must not outlive this script under ANY exit path — error, Ctrl+C, or success.
cleanup() {
  local rc=$?
  if [ -n "$ID" ]; then
    echo; echo "=== destroying $NAME ($ID) ==="
    doctl compute droplet delete "$ID" --force && echo "destroyed" || {
      echo "!!! DESTROY FAILED — $NAME ($ID) IS STILL BILLING. Destroy it by hand NOW:"
      echo "    doctl compute droplet delete $ID --force"
    }
  fi
  exit $rc
}
trap cleanup EXIT INT TERM

PRICE=$(doctl compute size list --format Slug,PriceHourly --no-header | awk -v s="$USE_SIZE" '$1==s{print $2}')
echo "!!! COST: $USE_SIZE at ~\$${PRICE:-?}/hr, per-second. Target lifetime ~6 min (~\$0.40). !!!"

ID=$(doctl compute droplet create "$NAME" \
  --region "$USE_REGION" --size "$USE_SIZE" --image "$IMAGE_SLUG" \
  --ssh-keys "$SSH_KEY_ID" --tag-name "$DRILL_TAG" \
  --wait --format ID --no-header)
IP=$(doctl compute droplet get "$ID" --format PublicIPv4 --no-header)
echo "created $NAME id=$ID ip=$IP"

# Marketplace images answer every command with a "Please wait..." placeholder that still exits 0,
# so assert on output content, not exit status.
echo "waiting for a real shell (max 15 min)..."
for i in $(seq 1 180); do
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 root@"$IP" \
      'echo PROBE_READY_$(id -u)' 2>/dev/null | grep -q 'PROBE_READY_0' && break
  [ "$i" = 180 ] && { echo "SSH never came up"; exit 1; }
  sleep 5
done

{
  echo "DigitalOcean spot-droplet metadata probe"
  echo "date    : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "droplet : $NAME ($ID)"
  echo "size    : $USE_SIZE   region: $USE_REGION   price: \$${PRICE}/hr"
  echo "question: does a SPOT instance expose any reclamation signal the on-demand one doesn't?"
  echo
} > "$OUT"

ssh root@"$IP" '
echo "=== confirm this really is a spot instance (from inside the box) ==="
curl -s http://169.254.169.254/metadata/v1/id; echo
echo
echo "=== metadata index ==="
curl -s http://169.254.169.254/metadata/v1/
echo
echo "=== full v1.json, every key (recursive) ==="
curl -s http://169.254.169.254/metadata/v1.json | python3 -c "
import json,sys
def walk(o,p=\"\"):
    if isinstance(o,dict):
        for k,v in sorted(o.items()): walk(v,f\"{p}.{k}\")
    elif isinstance(o,list):
        print(f\"{p}[] ({len(o)} items)\")
    else:
        s=str(o); print(f\"{p} = {s[:70]}\")
walk(json.load(sys.stdin))
"
echo
echo "=== AWS-equivalent paths (AWS uses /latest/meta-data/spot/instance-action) ==="
for p in spot spot/instance-action spot/termination-time instance-action reclaim reclamation \
         termination termination-time maintenance events scheduled-events interruption \
         preempt preemption lifecycle capacity notice; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://169.254.169.254/metadata/v1/$p")
  [ "$code" != "404" ] && echo "  !!! /metadata/v1/$p -> HTTP $code  <-- NOT 404, INVESTIGATE" \
                       || echo "      /metadata/v1/$p -> 404"
done
echo
echo "=== other metadata hosts some clouds use ==="
for h in 169.254.169.254/latest/meta-data/ 169.254.169.253/ 100.100.100.200/; do
  code=$(curl -s -m 3 -o /dev/null -w "%{http_code}" "http://$h" || echo "timeout")
  echo "      http://$h -> $code"
done
' 2>&1 | tee -a "$OUT"

echo
echo "=== what the DO API says about this droplet (any spot/reclaim field?) ==="
doctl compute droplet get "$ID" -o json | python3 -c "
import json,sys
d=json.load(sys.stdin)[0]
print('top-level keys:', sorted(d.keys()))
print()
print('size_slug   :', d.get('size_slug'))
print('size.descr  :', d.get('size',{}).get('description'))
hits=[k for k in d if any(w in k.lower() for w in ('spot','reclaim','preempt','interrupt','expire','terminat'))]
print()
print('fields mentioning spot/reclaim/preempt/interrupt/expire/terminate:', hits or 'NONE')
" | tee -a "$OUT"

echo
echo "probe written to $OUT"
