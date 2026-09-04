#!/usr/bin/env bash
# THE DRILL (phase 3): simulated notice -> drain -> replacement -> verified resume -> destroy.
# Also the Scene-8 "voluntary reclamation" — identical mechanics, different trigger.
# Every run is scoped by a fresh run_id; all waits are bounded; the old node's local
# records (timings.csv, losses.jsonl) are rescued BEFORE destroy.
# Usage: self_reclaim.sh drill <old-node-name>
set -euo pipefail
cd "$(dirname "$0")/.." && source .env
: "${CONTROLLER_IP:?}" ; : "${DRILL_TOKEN:?}"
MODE="${1:?usage: self_reclaim.sh drill <old-node>}"
OLD_NODE="${2:?}"
OLD_IP=$(cat ".state/$OLD_NODE.ip")
CTRL="http://$CONTROLLER_IP:$CONTROLLER_PORT"
AUTH=(-H "X-Drill-Token: $DRILL_TOKEN" -H 'Content-Type: application/json')
NEW_NODE="${OLD_NODE%%-*}-r$(date +%H%M%S)"
RUN_ID="drill-$(date +%s)"

trap 'echo "!! DRILL INTERRUPTED — droplets still billing:";
      doctl compute droplet list --tag-name "$DRILL_TAG" --format Name,Status,PublicIPv4 --no-header;
      echo "Old node was NOT destroyed (that only happens after RESUME OK)."' ERR

# Events are filtered by THIS run's id — stale events from prior runs can't satisfy waits
wait_for_event () { # $1=event name, $2=timeout seconds
  local deadline=$(( $(date +%s) + $2 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -sf "$CTRL/events" | python3 -c "
import sys, json
hits = [json.loads(l) for l in sys.stdin if l.strip()]
ok = any(e.get('event')=='$1' and e.get('run_id')=='$RUN_ID' for e in hits)
sys.exit(0 if ok else 1)"; then return 0; fi
    sleep 5
  done
  echo "TIMEOUT waiting for '$1' (${2}s). Nothing destroyed. Investigate:"; echo "  curl $CTRL/events | tail"
  return 1
}

echo "=== DRILL $RUN_ID | T0: reset + simulated reclamation notice for $OLD_NODE ==="
curl -sf -X POST "$CTRL/reset" "${AUTH[@]}" -d "{\"run_id\":\"$RUN_ID\"}" > /dev/null
# NOTE: agents poll /status, whose 'draining' answer isn't run-scoped — the reset above
# cleared prior drain state, so only THIS notice can trip them.
curl -sf -X POST "$CTRL/notice" "${AUTH[@]}" -d "{
  \"from\": \"no-reply@digitalocean.com (SIMULATED)\",
  \"subject\": \"Your Spot GPU Droplet will be reclaimed\",
  \"target_node\": \"$OLD_NODE\"
}" | tee /dev/stderr > /dev/null

echo "=== Waiting for node_safe (checkpoint written + uploaded; max 40 min) ==="
wait_for_event node_safe 2400
CKPT=$(curl -sf "$CTRL/events" | python3 -c "
import sys, json
ev = [json.loads(l) for l in sys.stdin if l.strip()]
print([e['ckpt'] for e in ev if e.get('event')=='node_safe' and e.get('run_id')=='$RUN_ID'][-1])")
echo "State durable in Spaces: $CKPT"
RESUME_STEP=$(echo "$CKPT" | sed 's/step0*//')

echo "=== Creating replacement (create BEFORE destroy — as-available!) ==="
./provision/01_create_spot.sh "$NEW_NODE" || {
  echo "$REGION dry — trying $FALLBACK_SIZE_SLUG in $FALLBACK_REGION (itself a finding; capture it).";
  echo "NOTE: the replacement is then a different GPU model — say so on screen if this fires.";
  ./provision/01_create_spot.sh "$NEW_NODE" "$FALLBACK_REGION";
}
NEW_IP=$(cat ".state/$NEW_NODE.ip")
./provision/02_bootstrap.sh "$NEW_IP"

echo "=== Pulling checkpoint on $NEW_NODE ($NEW_IP) — separate, measured stage ==="
ssh root@"$NEW_IP" "run_node.sh pull '$CKPT'"

echo "=== Resuming (+100 steps for the seamless-loss-curve footage) ==="
ssh root@"$NEW_IP" "run_node.sh train --resume auto --max-steps $((RESUME_STEP + 100))"

echo "=== Verifying RESUME OK (cursor + RNG assertions; max 20 min) ==="
wait_for_event resume_ok 1200
ssh root@"$NEW_IP" "grep 'RESUME OK' /var/log/train.log"

echo "=== Rescuing $OLD_NODE's local records before destroy ==="
mkdir -p "results/$OLD_NODE"
scp root@"$OLD_IP":/mnt/scratch/ckpt/timings.csv "results/$OLD_NODE/" || echo "WARN: timings.csv not rescued"
scp root@"$OLD_IP":/mnt/scratch/ckpt/losses.jsonl "results/$OLD_NODE/" || echo "WARN: losses.jsonl not rescued"

echo "=== Verified. NOW destroying $OLD_NODE (create -> verify -> destroy) ==="
./provision/03_destroy.sh "$OLD_NODE" --confirm
echo "=== DRILL $RUN_ID COMPLETE. Timeline: curl $CTRL/events | grep $RUN_ID ==="
echo "Reminder: trainer on $NEW_NODE stops itself at step $((RESUME_STEP + 100)); run 'run_node.sh stop' before phase 5."
