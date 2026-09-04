#!/usr/bin/env python3
"""Pane B for the drill: the controller's event log as a readable T-minus timeline.

`curl /events | tail -20` puts raw JSON and epoch-ms on camera, mixed across every run the
controller has ever seen. This shows ONE run, relative to the notice, in the order a viewer
reads it — which is the Scene 7 timeline.

All offsets come from the controller's server-side t_ms. Never compute cross-node intervals
from node clocks; two droplets don't agree closely enough to quote seconds on screen.

Usage:  timeline.py            # current run (since the last /reset)
        timeline.py --all      # every event the controller holds
"""
import json, os, sys, urllib.request
from datetime import datetime, timezone

CTRL = f"http://{os.environ['CONTROLLER_IP']}:{os.environ.get('CONTROLLER_PORT', '8080')}"

raw = urllib.request.urlopen(f"{CTRL}/events", timeout=5).read().decode()
ev = [json.loads(l) for l in raw.splitlines() if l.strip()]
if not ev:
    sys.exit("no events on the controller yet")

if "--all" not in sys.argv:
    run = ev[-1].get("run_id")
    ev = [e for e in ev if e.get("run_id") == run]
else:
    run = "ALL"

gib = lambda b: f"{int(b)/2**30:.1f} GiB"


def detail(e):
    k = e.get("event")
    if k == "notice_received":
        return e.get("target_node", "")
    if k in ("ckpt_written", "node_safe"):
        return f"{e.get('ckpt','')}  (+{e.get('since_notice_s','?')}s since notice)"
    if k == "provisioned":
        return (f"{e.get('size','')} {e.get('region','')} "
                f"create->gpu {e.get('create_to_gpu_s','?')}s")
    if k in ("ckpt_upload", "ckpt_download"):
        return f"{gib(e['bytes'])} in {e.get('dur_s','?')}s = {e.get('gbps','?')} Gbps"
    if k and k.startswith("ckpt_write"):
        return f"{gib(e['bytes'])} in {e.get('dur_s','?')}s"
    if k == "resume_ok":
        return f"step={e.get('step','?')}"
    if k == "drain_failed":
        return f"!! {e.get('reason','')}"
    return ""


# T0 is the notice if there is one — that's what the video counts from.
t0 = next((e["t_ms"] for e in ev if e.get("event") == "notice_received"), ev[0]["t_ms"])
when = datetime.fromtimestamp(t0 / 1000, timezone.utc).strftime("%H:%M:%SZ")
print(f"run {run}   T0 = {when}\n")
for e in ev:
    d = (e["t_ms"] - t0) / 1000
    sign = "-" if d < 0 else "+"
    stamp = f"T{sign}{int(abs(d)) // 60:02d}:{int(abs(d)) % 60:02d}"
    print(f"  {stamp}  {e.get('event',''):<17} {e.get('node') or e.get('target_node',''):<14} {detail(e)}")
