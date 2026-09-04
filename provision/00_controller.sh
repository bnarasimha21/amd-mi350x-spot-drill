#!/usr/bin/env bash
# Create the persistent controller droplet: Redis (password) + notice webhook (token) + event log.
# Controller gets its OWN tag (${DRILL_TAG}-ctrl) so 03_destroy.sh --all can never take it
# (and events.jsonl) down with the GPU fleet.
set -euo pipefail
cd "$(dirname "$0")/.." && source .env
export DIGITALOCEAN_ACCESS_TOKEN="$DO_TOKEN"
: "${DRILL_TOKEN:?generate one: openssl rand -hex 16, put in .env}"
: "${REDIS_PASSWORD:?generate one: openssl rand -hex 16, put in .env}"
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
# Redis binds 0.0.0.0 and the webhook is token-only; both must be reachable by the GPU
# fleet (dynamic IPs -> source by TAG, which DO resolves natively) and by this laptop.
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
fw_ensure "${CTRL_TAG}-fw" "$CTRL_TAG" "protocol:tcp,ports:22,address:$MY_IP/32 \
protocol:tcp,ports:8080,address:$MY_IP/32 \
protocol:tcp,ports:8080,tag:$DRILL_TAG \
protocol:tcp,ports:6379,address:$MY_IP/32 \
protocol:tcp,ports:6379,tag:$DRILL_TAG \
protocol:icmp,address:$MY_IP/32"

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

echo "Installing Redis (password) + webhook (token) ..."
scp drain/notice_webhook.py root@"$IP":/opt/notice_webhook.py
ssh root@"$IP" DRILL_TOKEN="$DRILL_TOKEN" REDIS_PASSWORD="$REDIS_PASSWORD" bash -s <<'REMOTE'
set -e
apt-get update -qq && apt-get install -y -qq redis-server python3-flask
sed -i 's/^bind .*/bind 0.0.0.0/' /etc/redis/redis.conf
echo "requirepass $REDIS_PASSWORD" >> /etc/redis/redis.conf
systemctl restart redis-server
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
ssh root@"$IP" 'redis-cli ping 2>&1 | head -1' | grep -q NOAUTH \
  && echo "redis auth OK (unauthenticated PING -> NOAUTH)" \
  || echo "!! WARNING: redis answered without a password"
echo "Controller ready at $IP — CONTROLLER_IP is already in .env."
