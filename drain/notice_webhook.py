#!/usr/bin/env python3
"""Controller-side notice webhook + event log.

In production this endpoint would be the target of an inbound-mail hook (the reclamation
notice arrives by email in the current preview). For the drill we POST to it directly with
a payload shaped like a forwarded email.

Hardening (this box is internet-facing):
- Every POST requires header X-Drill-Token == DRILL_TOKEN (env, baked in by 00_controller.sh).
- Drains are scoped per node: /notice names a target node; /status?node=x answers for x only.
- Every event is stamped with the active run_id (set by /reset) and a server-side t_ms —
  cross-node intervals must ONLY be computed from these single-clock t_ms values.
"""
import json, os, time, uuid
from flask import Flask, request, jsonify, abort

app = Flask(__name__)
TOKEN = os.environ.get("DRILL_TOKEN", "")
STATE = {"run_id": None, "draining_nodes": [], "notice_at": None}
LOG = "/opt/events.jsonl"


def check_token():
    if not TOKEN or request.headers.get("X-Drill-Token") != TOKEN:
        abort(401)


def log_event(payload):
    payload["t_ms"] = int(time.time() * 1000)
    payload["run_id"] = STATE["run_id"]
    with open(LOG, "a") as f:
        f.write(json.dumps(payload) + "\n")
    return payload


@app.post("/reset")
def reset():
    check_token()
    body = request.get_json(silent=True) or {}
    STATE.update(run_id=body.get("run_id") or uuid.uuid4().hex[:8],
                 draining_nodes=[], notice_at=None)
    log_event({"event": "reset"})
    return jsonify(STATE)


@app.post("/notice")
def notice():
    check_token()
    body = request.get_json(silent=True) or {}
    node = body.get("target_node", "*")  # real reclamation emails name a specific droplet
    STATE["draining_nodes"].append(node)
    STATE["notice_at"] = int(time.time() * 1000)
    log_event({"event": "notice_received", "target_node": node, "payload": body})
    return jsonify(ok=True, draining=STATE["draining_nodes"])


@app.get("/status")
def status():
    node = request.args.get("node", "*")
    draining = node in STATE["draining_nodes"] or "*" in STATE["draining_nodes"]
    return jsonify(status="draining" if draining else "running",
                   run_id=STATE["run_id"], notice_at=STATE["notice_at"])


@app.post("/event")
def event():
    check_token()
    return jsonify(log_event(request.get_json(force=True)))


@app.get("/events")
def events():
    try:
        return "".join(open(LOG)), 200, {"Content-Type": "application/x-ndjson"}
    except FileNotFoundError:
        return "", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
