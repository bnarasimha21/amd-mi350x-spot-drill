#!/usr/bin/env bash
# End-to-end self-test of the serving tools against the FAKE server. Offline, no GPU.
# It checks that the pieces fit together. The numbers it prints time a sleep loop and are
# marked FAKE on every line. They are not serving numbers.
#
# Usage: serving/selftest.sh
set -euo pipefail
cd "$(dirname "$0")/.."
TMP="$(mktemp -d)"; PORT_FILE="$TMP/port"
cleanup () { [ -n "${PID:-}" ] && kill "$PID" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

python3 - "$PORT_FILE" <<'PY' &
import sys
sys.path.insert(0, "serving")
import fake_openai_server
srv = fake_openai_server.make_server(port=0, ttft_s=0.02, token_s=0.002)
open(sys.argv[1], "w").write(str(srv.server_address[1]))
srv.serve_forever()
PY
PID=$!
for i in $(seq 1 50); do [ -s "$PORT_FILE" ] && break; sleep 0.1; done
[ -s "$PORT_FILE" ] || { echo "fake server did not start"; exit 1; }
URL="http://127.0.0.1:$(cat "$PORT_FILE")"

python3 serving/loadgen.py --base-url "$URL" --model fake-model --concurrency 1,4 \
  --duration 2 --pause 0 --max-tokens 16 --hardware "none: fake server" --out "$TMP/fake.json"

python3 - "$TMP/fake.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
assert r["is_fake_server"] is True and r["publishable"] is False, "fake result not marked fake"
assert all(l["requests_ok"] > 0 and l["requests_failed"] == 0 for l in r["levels"])
print("loadgen result is marked fake and not publishable: ok")
PY

# The calculator must refuse a fake result by default...
if python3 serving/cost_per_mtok.py "$TMP/fake.json" --price-hourly 1 >/dev/null 2>&1; then
  echo "FAIL: cost_per_mtok.py accepted a fake result without --allow-fake"; exit 1
fi
echo "cost calculator refuses the fake result by default: ok"
# ...and mark every line when told to go ahead. The price of 1 is a fixture, not a quote.
python3 serving/cost_per_mtok.py "$TMP/fake.json" --price-hourly 1 \
  --price-source "selftest fixture, not a price" --allow-fake --target-per-mtok 1
echo "SELFTEST OK"
