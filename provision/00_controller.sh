#!/usr/bin/env bash
# Create the persistent controller droplet: notice webhook (token) + event log, and
# optionally Redis (password) for queue_demo/. Redis is OFF by default: set ENABLE_REDIS=1
# (in .env or the environment; a value in the environment wins over .env) to install it.
# It binds 0.0.0.0, so keep the firewall on. REDIS_PASSWORD is needed only when
# ENABLE_REDIS=1.
# Controller gets its OWN tag (${DRILL_TAG}-ctrl) so 03_destroy.sh --all can never take it
# (and events.jsonl) down with the GPU fleet.
set -euo pipefail
cd "$(dirname "$0")/.."
# An ENABLE_REDIS passed in the environment wins over .env (source would clobber it).
_ENV_ENABLE_REDIS="${ENABLE_REDIS-}"
source .env
if [ -n "$_ENV_ENABLE_REDIS" ]; then ENABLE_REDIS="$_ENV_ENABLE_REDIS"; fi
ENABLE_REDIS="${ENABLE_REDIS:-0}"
case "$ENABLE_REDIS" in
  0|1) ;;
  *) echo "WARN: ENABLE_REDIS='$ENABLE_REDIS' is not 0 or 1; treating it as 0 (Redis NOT installed). Set ENABLE_REDIS=1 to enable." >&2
     ENABLE_REDIS=0 ;;
esac
export DIGITALOCEAN_ACCESS_TOKEN="$DO_TOKEN"
: "${DRILL_TOKEN:?generate one: openssl rand -hex 16, put in .env}"
if [ "$ENABLE_REDIS" = "1" ]; then
  : "${REDIS_PASSWORD:?ENABLE_REDIS=1 needs REDIS_PASSWORD: openssl rand -hex 16, put in .env}"
fi
CTRL_TAG="${DRILL_TAG}-ctrl"
CTRL_REGION="${CONTROLLER_REGION:-$REGION}"  # if basic droplets don't exist in the GPU region, set CONTROLLER_REGION=nyc3

# Both tags must EXIST before any firewall can reference them (DO 422s on unknown tags),
# and $DRILL_TAG has no members until the first GPU droplet — so create it empty, up front.
for t in "$CTRL_TAG" "$DRILL_TAG"; do
  doctl compute tag get "$t" >/dev/null 2>&1 || doctl compute tag create "$t" >/dev/null
done

# Idempotent: re-running after a partial failure must not create a second controller.
ID=$(doctl compute droplet list --tag-name "$CTRL_TAG" --format ID,Name --no-header \
     | awk '$2=="spot-drill-controller"{print $1}' | head -1)
if [ -n "$ID" ]; then
  echo "Reusing existing controller droplet id=$ID"
else
  echo "Creating controller droplet (s-1vcpu-1gb, ~\$6/mo prorated per-second) in $CTRL_REGION..."
  ID=$(doctl compute droplet create spot-drill-controller \
    --region "$CTRL_REGION" --size s-1vcpu-1gb --image ubuntu-24-04-x64 \
    --ssh-keys "$SSH_KEY_ID" --tag-name "$CTRL_TAG" \
    --wait --format ID --no-header)
fi
IP=$(doctl compute droplet get "$ID" --format PublicIPv4 --no-header)
mkdir -p .state && echo "$ID" > .state/controller.id
python3 - "$IP" <<'PY'
import pathlib, re, sys
p = pathlib.Path(".env")
p.write_text(re.sub(r'(?m)^CONTROLLER_IP=.*$', 'CONTROLLER_IP=' + sys.argv[1], p.read_text()))
PY
echo "Controller: id=$ID ip=$IP  (CONTROLLER_IP written to .env)"

# --- Cloud Firewall ---------------------------------------------------------
# The webhook is token-only (and Redis too, when ENABLE_REDIS=1, bound to 0.0.0.0); both
# must be reachable by the GPU fleet (dynamic IPs -> source by TAG, which DO resolves natively) and by this laptop.
# MY_IP is captured at create time; if your network changes, update the firewall:
#   doctl compute firewall list  &&  doctl compute firewall update <id> ...
MY_IP=$(curl -sf https://ipinfo.io/ip || true)
[ -n "$MY_IP" ] || echo "WARN: could not determine public IP — SKIPPING firewalls, ports stay OPEN"

fw_ensure () {  # $1=firewall name  $2=tag to attach to  $3=inbound rules
  if doctl compute firewall list --format Name --no-header | grep -qx "$1"; then
    echo "Firewall $1 already exists — leaving it alone."; return
  fi
  [ -n "$MY_IP" ] || return 0
  echo "Creating firewall $1 on tag:$2 (admin source $MY_IP/32)..."
  doctl compute firewall create --name "$1" --tag-names "$2" \
    --inbound-rules "$3" \
    --outbound-rules "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0 \
protocol:udp,ports:all,address:0.0.0.0/0,address:::/0 \
protocol:icmp,address:0.0.0.0/0,address:::/0" \
    --format ID,Name,Status --no-header
}

# Controller: /events and /status are UNAUTHENTICATED GETs, so this firewall is the only
# thing keeping the measurement log private. GPU fleet reaches it by tag (dynamic IPs).
CTRL_RULES="protocol:tcp,ports:22,address:$MY_IP/32 \
protocol:tcp,ports:8080,address:$MY_IP/32 \
protocol:tcp,ports:8080,tag:$DRILL_TAG \
protocol:icmp,address:$MY_IP/32"
if [ "$ENABLE_REDIS" = "1" ]; then
  CTRL_RULES="$CTRL_RULES \
protocol:tcp,ports:6379,address:$MY_IP/32 \
protocol:tcp,ports:6379,tag:$DRILL_TAG"
fi
fw_ensure "${CTRL_TAG}-fw" "$CTRL_TAG" "$CTRL_RULES"

# fw_ensure leaves an existing firewall alone, so toggling ENABLE_REDIS after the first run
# must add or remove the 6379 rules here (idempotent: checks the current rules first).
REDIS_RULES="protocol:tcp,ports:6379,address:$MY_IP/32 protocol:tcp,ports:6379,tag:$DRILL_TAG"
fw_redis_sync () {  # $1=firewall name
  [ -n "$MY_IP" ] || return 0
  local fid has
  fid=$(doctl compute firewall list --format ID,Name --no-header | awk -v n="$1" '$2==n{print $1}' | head -1)
  [ -n "$fid" ] || return 0
  has=$(doctl compute firewall get "$fid" --format InboundRules --no-header | grep -c 'ports:6379' || true)
  if [ "$ENABLE_REDIS" = "1" ] && [ "${has:-0}" = "0" ]; then
    echo "Opening 6379 on firewall $1 (ENABLE_REDIS=1)..."
    doctl compute firewall add-rules "$fid" --inbound-rules "$REDIS_RULES" \
      || echo "WARN: could not open 6379; add inbound tcp 6379 (your IP and tag:$DRILL_TAG) to $1 by hand" >&2
  elif [ "$ENABLE_REDIS" != "1" ] && [ "${has:-0}" != "0" ]; then
    echo "Closing 6379 on firewall $1 (ENABLE_REDIS=0)..."
    doctl compute firewall remove-rules "$fid" --inbound-rules "$REDIS_RULES" \
      || echo "WARN: could not remove the 6379 rules (your IP may have changed); remove inbound tcp 6379 from $1 by hand" >&2
  fi
}
fw_redis_sync "${CTRL_TAG}-fw"

# GPU fleet: SSH from this laptop only. The DO AMD image publishes a Jupyter login on 8888
# (plus 8000/30000) to 0.0.0.0 — an exposed Jupyter on a GPU box is a mining-bot magnet.
# Tag-scoped, so the mid-drill replacement droplet inherits it without anyone remembering to.
fw_ensure "${DRILL_TAG}-fw" "$DRILL_TAG" "protocol:tcp,ports:22,address:$MY_IP/32 \
protocol:icmp,address:$MY_IP/32"

echo "Waiting for SSH (max 10 min)..."
for i in $(seq 1 120); do
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 root@"$IP" true 2>/dev/null && break
  [ "$i" = 120 ] && { echo "SSH timeout — droplet $ID still exists; investigate or destroy"; exit 1; }
  sleep 5
done

echo "Installing webhook (token)$([ "$ENABLE_REDIS" = "1" ] && echo ' + Redis (password)') ..."
scp drain/notice_webhook.py root@"$IP":/opt/notice_webhook.py
ssh root@"$IP" DRILL_TOKEN="$DRILL_TOKEN" ENABLE_REDIS="$ENABLE_REDIS" REDIS_PASSWORD="${REDIS_PASSWORD:-}" bash -s <<'REMOTE'
set -e
apt-get update -qq && apt-get install -y -qq python3-flask
if [ "$ENABLE_REDIS" = "1" ]; then
  apt-get install -y -qq redis-server
  sed -i 's/^bind .*/bind 0.0.0.0/' /etc/redis/redis.conf
  echo "requirepass $REDIS_PASSWORD" >> /etc/redis/redis.conf
  systemctl restart redis-server
fi
cat > /etc/systemd/system/notice-webhook.service <<UNIT
[Unit]
Description=Simulated reclamation notice webhook
[Service]
Environment=DRILL_TOKEN=$DRILL_TOKEN
ExecStart=/usr/bin/python3 /opt/notice_webhook.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable --now notice-webhook
REMOTE

echo "Verifying controller surfaces..."
for i in $(seq 1 24); do
  curl -sf -m 5 -X POST "http://$IP:8080/reset" \
    -H "X-Drill-Token: $DRILL_TOKEN" -H 'Content-Type: application/json' -d '{}' && break
  [ "$i" = 24 ] && { echo "!! webhook never answered. Droplet $ID is up; check: ssh root@$IP journalctl -u notice-webhook -n50"; exit 1; }
  sleep 5
done
echo
# An unauthenticated call must be rejected — the token is the only thing guarding /notice.
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST "http://$IP:8080/reset" \
  -H 'Content-Type: application/json' -d '{}')
[ "$CODE" = "401" ] || [ "$CODE" = "403" ] \
  && echo "auth check OK (unauthenticated /reset -> HTTP $CODE)" \
  || echo "!! WARNING: unauthenticated /reset returned HTTP $CODE — the webhook is NOT guarded"
if [ "$ENABLE_REDIS" = "1" ]; then
  ssh root@"$IP" 'redis-cli ping 2>&1 | head -1' | grep -q NOAUTH \
    && echo "redis auth OK (unauthenticated PING -> NOAUTH)" \
    || echo "!! WARNING: redis answered without a password"
fi
echo "Controller ready at $IP — CONTROLLER_IP is already in .env."
