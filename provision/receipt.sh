#!/usr/bin/env bash
# Print DO's OWN record of a drill droplet: size slug, plan name, posted price, created_at.
#
# Why this exists: the control panel does NOT label a spot droplet as spot anywhere after
# creation — not in Configuration Details, not in the GPU Droplets list. The API does, in
# DO's own words ("AMD MI350X - 1X Spot"). This is the written proof of the create mechanism
# for Scene 2, and the same-day billing receipt for Phase 6 (run it BEFORE destroying).
#
# Do NOT use our own `spot-drill` tag as evidence of anything — we set that tag ourselves.
#
# Usage: receipt.sh <droplet-name> | receipt.sh --all
set -euo pipefail
cd "$(dirname "$0")/.." && source .env
export DIGITALOCEAN_ACCESS_TOKEN="$DO_TOKEN"

if [ "${1:?usage: receipt.sh <droplet-name>|--all}" = "--all" ]; then
  JSON=$(doctl compute droplet list --tag-name "$DRILL_TAG" -o json)
else
  JSON=$(doctl compute droplet get "$1" -o json)
fi

echo "$JSON" | python3 -c '
import sys, json
for d in json.load(sys.stdin):
    s = d["size"]
    print(f"""droplet    : {d["name"]}
region     : {d["region"]["slug"]}
size slug  : {d["size_slug"]}
plan       : {s["description"]}
price      : ${s["price_hourly"]:.2f} / hour  (per-second billing)
image      : {d["image"]["name"]}
created    : {d["created_at"]}
""")'
