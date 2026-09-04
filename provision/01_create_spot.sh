#!/usr/bin/env bash
# Create a spot MI350X droplet and MEASURE: create -> SSH-able -> GPU visible.
# Resolves droplets by ID (names aren't unique on DO); writes .state/<name>.{id,ip}.
# Usage: 01_create_spot.sh <name> [region]
set -euo pipefail
cd "$(dirname "$0")/.." && source .env
export DIGITALOCEAN_ACCESS_TOKEN="$DO_TOKEN"
: "${CONTROLLER_IP:?run 00_controller.sh first and fill CONTROLLER_IP in .env}"
NAME="${1:?usage: 01_create_spot.sh <name> [region]}"
USE_REGION="${2:-$REGION}"
mkdir -p .state

trap 'echo "!! If this failed mid-create, check for billing droplets:";
      doctl compute droplet list --tag-name "$DRILL_TAG" --format ID,Name,Status,PublicIPv4' ERR

# Refuse duplicates: a leftover same-named droplet means a failed prior run — clean it first
if doctl compute droplet list --tag-name "$DRILL_TAG" --format Name --no-header | grep -qx "$NAME"; then
  echo "A drill droplet named '$NAME' already exists. Destroy it or pick another name."; exit 1
fi

# Spot capacity is selected by the size slug itself (there is no --spot flag on doctl).
# Each spot GPU is offered in exactly one region, so the region choice picks the slug:
# MI350X 1x -> ric1 only; MI355X 1x -> mem1 only.
if [ "$USE_REGION" = "$FALLBACK_REGION" ]; then
  USE_SIZE="$FALLBACK_SIZE_SLUG"
else
  USE_SIZE="$SPOT_SIZE_SLUG"
fi
PRICE=$(doctl compute size list --format Slug,PriceHourly --no-header | awk -v s="$USE_SIZE" '$1==s{print $2}')

echo "!!! COST: $USE_SIZE bills ~\$${PRICE:-?}/hr (today's posted price; per-second) !!!"
echo "Creating $NAME in $USE_REGION..."
T_CREATE=$(date +%s)

ID=$(doctl compute droplet create "$NAME" \
  --region "$USE_REGION" --size "$USE_SIZE" --image "$IMAGE_SLUG" \
  --ssh-keys "$SSH_KEY_ID" --tag-name "$DRILL_TAG" \
  --wait --format ID --no-header)
IP=$(doctl compute droplet get "$ID" --format PublicIPv4 --no-header)
echo "$ID" > ".state/$NAME.id"; echo "$IP" > ".state/$NAME.ip"
echo "PHASE-0 GATE REMINDER: verify on the DO console that $NAME shows the SPOT billing tier."

# The DO marketplace images gate the shell behind a first-boot script that answers EVERY
# command with "Please wait while we get your droplet ready..." and still exits 0. So both
# waits must assert on OUTPUT, never on exit status, or they pass ~75s early against a
# placeholder shell and the measurement is fiction.
echo "Waiting for a real shell on $IP (max 15 min)..."
for i in $(seq 1 180); do
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 root@"$IP" \
      'echo DRILL_SHELL_READY_$(id -u)' 2>/dev/null | grep -q 'DRILL_SHELL_READY_0' && break
  [ "$i" = 180 ] && { echo "SSH timeout. Droplet $NAME ($ID) IS BILLING. Destroy: ./provision/03_destroy.sh $NAME --confirm"; exit 1; }
  sleep 5
done
T_SSH=$(date +%s)

echo "Waiting for GPU (max 10 min)..."
for i in $(seq 1 120); do
  ssh root@"$IP" 'PATH=$PATH:/opt/rocm/bin rocm-smi --showproductname' 2>/dev/null \
    | grep -qi 'Card Series' && break
  [ "$i" = 120 ] && { echo "GPU never visible. Droplet $NAME ($ID) IS BILLING. Destroy: ./provision/03_destroy.sh $NAME --confirm"; exit 1; }
  sleep 5
done
T_GPU=$(date +%s)

echo "== PROVISION TIMINGS ($NAME): create->ssh $((T_SSH - T_CREATE))s, create->gpu $((T_GPU - T_CREATE))s =="
curl -s -X POST "http://$CONTROLLER_IP:$CONTROLLER_PORT/event" \
  -H 'Content-Type: application/json' -H "X-Drill-Token: $DRILL_TOKEN" \
  -d "{\"event\":\"provisioned\",\"node\":\"$NAME\",\"region\":\"$USE_REGION\",\"size\":\"$USE_SIZE\",\"price_hourly\":\"${PRICE:-}\",\"create_to_ssh_s\":$((T_SSH-T_CREATE)),\"create_to_gpu_s\":$((T_GPU-T_CREATE))}" \
  || echo "WARN: provision event not recorded on controller"
echo "$NAME ready at $IP"
