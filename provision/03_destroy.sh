#!/usr/bin/env bash
# Destroy drill GPU droplets. NEVER touches the controller (own tag: ${DRILL_TAG}-ctrl).
# --all exports events.jsonl from the controller FIRST — measurements outlive the fleet.
# Usage: 03_destroy.sh <name> --confirm | --all --confirm [--force-anyway]
set -euo pipefail
cd "$(dirname "$0")/.." && source .env
export DIGITALOCEAN_ACCESS_TOKEN="$DO_TOKEN"

TARGET="${1:?usage: 03_destroy.sh <name> --confirm | --all --confirm [--force-anyway]}"
[ "${2:-}" = "--confirm" ] || {
  echo "Refusing to delete cloud resources without --confirm."
  exit 1
}

if [ "$TARGET" = "--all" ]; then
  mkdir -p results
  if [ -n "${CONTROLLER_IP:-}" ]; then
    OUT="results/events-$(date +%Y%m%d-%H%M%S).jsonl"
    curl -sf "http://$CONTROLLER_IP:$CONTROLLER_PORT/events" -o "$OUT" \
      && echo "events.jsonl exported → $OUT" \
      || { echo "!! Could not export events.jsonl — aborting destroy. (--force-anyway to override)";
           [ "${3:-}" = "--force-anyway" ] || exit 1; }
  fi
  echo "About to destroy these droplets (controller NOT included):"
  doctl compute droplet list --tag-name "$DRILL_TAG" --format ID,Name,Status --no-header
  echo "Proceeding in 5s (ctrl-c to abort)..."; sleep 5
  doctl compute droplet delete --tag-name "$DRILL_TAG" --force
else
  ID=$(cat ".state/$TARGET.id" 2>/dev/null || doctl compute droplet list --tag-name "$DRILL_TAG" \
       --format ID,Name --no-header | awk -v n="$TARGET" '$2==n{print $1}' | head -1)
  [ -n "$ID" ] || { echo "No drill-tagged droplet named '$TARGET'"; exit 1; }
  doctl compute droplet delete "$ID" --force
  rm -f ".state/$TARGET.id" ".state/$TARGET.ip"
  echo "Destroyed $TARGET (id $ID) — per-second billing stops now."
  curl -s -X POST "http://$CONTROLLER_IP:$CONTROLLER_PORT/event" \
    -H 'Content-Type: application/json' -H "X-Drill-Token: $DRILL_TOKEN" \
    -d "{\"event\":\"destroyed\",\"node\":\"$TARGET\"}" || true
fi
