#!/usr/bin/env python3
"""Record what the cluster does during the autoscaling and reclamation tests, on one clock.

The drill timed every stage against the controller's clock (docs/DESIGN.md, D6). The serving
tests need the same thing: "queue built up", "new node appeared", "node Ready", "pod
scheduled", "pod Ready" all happen on different machines. This script polls the cluster with
kubectl every few seconds from the operator's machine and writes one JSON line per change,
stamped with this machine's clock. Resolution is the poll interval, not better.

    python3 serving/scale_watch.py watch --out results/serving/autoscale-events.jsonl
    python3 serving/scale_watch.py summary results/serving/autoscale-events.jsonl

`watch` runs until Ctrl-C or --max-minutes. It only reads. `summary` prints the intervals
from the first poll at or above --inflight-threshold to each later milestone.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

INFLIGHT_QUERY = "sum(vllm:num_requests_waiting) + sum(vllm:num_requests_running)"


def _cond(obj: dict, kind: str) -> bool:
    return any(c.get("type") == kind and c.get("status") == "True"
               for c in (obj.get("status") or {}).get("conditions") or [])


def snapshot_from(nodes: dict, pods: dict, deploy: dict | None, inflight: float | None) -> dict:
    """Reduce raw kubectl JSON to the few facts whose changes we time."""
    snap = {"nodes": {}, "pods": {}, "spec_replicas": None, "ready_replicas": None,
            "inflight": inflight}
    for n in nodes.get("items", []):
        snap["nodes"][n["metadata"]["name"]] = {
            "ready": _cond(n, "Ready"),
            "unschedulable": bool((n.get("spec") or {}).get("unschedulable")),
        }
    for p in pods.get("items", []):
        snap["pods"][p["metadata"]["name"]] = {
            "node": (p.get("spec") or {}).get("nodeName"),
            "ready": _cond(p, "Ready"),
            "terminating": bool(p["metadata"].get("deletionTimestamp")),
        }
    if deploy:
        snap["spec_replicas"] = (deploy.get("spec") or {}).get("replicas")
        snap["ready_replicas"] = (deploy.get("status") or {}).get("readyReplicas") or 0
    return snap


def diff(prev: dict | None, cur: dict, threshold: float | None) -> list[dict]:
    """Events that turn `prev` into `cur`. With prev=None, describe the starting state."""
    ev = []
    if prev is None:
        return [{"event": "watch_started", "nodes": sorted(cur["nodes"]),
                 "pods": sorted(cur["pods"]), "spec_replicas": cur["spec_replicas"],
                 "ready_replicas": cur["ready_replicas"], "inflight": cur["inflight"]}]
    for name, n in cur["nodes"].items():
        old = prev["nodes"].get(name)
        if old is None:
            ev.append({"event": "node_seen", "node": name})
            old = {"ready": False, "unschedulable": False}
        if n["ready"] != old["ready"]:
            ev.append({"event": "node_ready" if n["ready"] else "node_not_ready", "node": name})
        if n["unschedulable"] != old["unschedulable"]:
            ev.append({"event": "node_cordoned" if n["unschedulable"] else "node_uncordoned",
                       "node": name})
    for name in prev["nodes"]:
        if name not in cur["nodes"]:
            ev.append({"event": "node_gone", "node": name})
    for name, p in cur["pods"].items():
        old = prev["pods"].get(name)
        if old is None:
            ev.append({"event": "pod_seen", "pod": name})
            old = {"node": None, "ready": False, "terminating": False}
        if p["node"] and not old["node"]:
            ev.append({"event": "pod_scheduled", "pod": name, "node": p["node"]})
        if p["terminating"] and not old["terminating"]:
            ev.append({"event": "pod_terminating", "pod": name})
        if p["ready"] != old["ready"]:
            ev.append({"event": "pod_ready" if p["ready"] else "pod_not_ready", "pod": name})
    for name in prev["pods"]:
        if name not in cur["pods"]:
            ev.append({"event": "pod_gone", "pod": name})
    if cur["spec_replicas"] != prev["spec_replicas"]:
        ev.append({"event": "replicas_wanted_changed", "from": prev["spec_replicas"],
                   "to": cur["spec_replicas"]})
    if cur["ready_replicas"] != prev["ready_replicas"]:
        ev.append({"event": "replicas_ready_changed", "from": prev["ready_replicas"],
                   "to": cur["ready_replicas"]})
    if threshold is not None and cur["inflight"] is not None:
        was = prev["inflight"] is not None and prev["inflight"] >= threshold
        now = cur["inflight"] >= threshold
        if now != was:
            ev.append({"event": "inflight_above_threshold" if now else "inflight_below_threshold",
                       "inflight": cur["inflight"], "threshold": threshold})
    return ev


def parse_prom_scalar(raw: str) -> float | None:
    """Value of an instant query from the Prometheus HTTP API. None if empty or malformed."""
    try:
        d = json.loads(raw)
        res = d["data"]["result"]
        if d["data"]["resultType"] == "scalar":
            return float(res[1])
        return float(res[0]["value"][1]) if res else None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def kubectl_json(args: list[str], timeout=20) -> dict | None:
    try:
        out = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def poll(a) -> dict | None:
    nodes = kubectl_json(["get", "nodes", "-l", a.node_selector, "-o", "json"])
    pods = kubectl_json(["get", "pods", "-n", a.namespace, "-l", a.pod_selector, "-o", "json"])
    deploy = kubectl_json(["get", "deployment", a.deployment, "-n", a.namespace, "-o", "json"])
    if nodes is None or pods is None:
        return None
    inflight = None
    if a.prometheus_service:
        path = (f"/api/v1/namespaces/{a.namespace}/services/{a.prometheus_service}/proxy"
                f"/api/v1/query?query={quote(a.query)}")
        try:
            out = subprocess.run(["kubectl", "get", "--raw", path], capture_output=True,
                                 text=True, timeout=20)
            inflight = parse_prom_scalar(out.stdout) if out.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            inflight = None
    return snapshot_from(nodes, pods, deploy, inflight)


def cmd_watch(a) -> int:
    deadline = time.time() + a.max_minutes * 60
    prev, misses = None, 0
    print(f"[watch] polling every {a.interval}s for at most {a.max_minutes} min -> {a.out}",
          file=sys.stderr)
    with open(a.out, "a", encoding="utf-8") as f:
        def emit(e):
            e = {"t": time.time(), "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 **e}
            f.write(json.dumps(e) + "\n")
            f.flush()
            print("[watch] " + json.dumps(e), file=sys.stderr, flush=True)
        try:
            while time.time() < deadline:
                cur = poll(a)
                if cur is None:
                    misses += 1
                    if misses == 3:
                        emit({"event": "poll_failing", "note": "kubectl returned no data 3 times"})
                else:
                    misses = 0
                    for e in diff(prev, cur, a.inflight_threshold):
                        emit(e)
                    prev = cur
                time.sleep(a.interval)
            emit({"event": "watch_stopped", "reason": "max_minutes"})
        except KeyboardInterrupt:
            emit({"event": "watch_stopped", "reason": "interrupted"})
    return 0


def summarise(events: list[dict]) -> dict:
    """Seconds from the first inflight_above_threshold to the first of each later milestone."""
    t0 = next((e["t"] for e in events if e["event"] == "inflight_above_threshold"), None)
    out = {"t0_event": "inflight_above_threshold", "t0_found": t0 is not None, "intervals_s": {}}
    if t0 is None:
        return out
    after = [e for e in events if e["t"] >= t0]
    firsts = {
        "replicas_wanted_increased": lambda e: e["event"] == "replicas_wanted_changed"
        and (e.get("to") or 0) > (e.get("from") or 0),
        "new_node_seen": lambda e: e["event"] == "node_seen",
        "new_node_ready": lambda e: e["event"] == "node_ready",
        "new_pod_scheduled": lambda e: e["event"] == "pod_scheduled",
        "new_pod_ready": lambda e: e["event"] == "pod_ready",
        "replicas_ready_increased": lambda e: e["event"] == "replicas_ready_changed"
        and (e.get("to") or 0) > (e.get("from") or 0),
    }
    for name, match in firsts.items():
        hit = next((e for e in after if match(e)), None)
        out["intervals_s"][name] = None if hit is None else round(hit["t"] - t0, 1)
    return out


def cmd_summary(a) -> int:
    events = [json.loads(l) for l in open(a.events, encoding="utf-8") if l.strip()]
    s = summarise(events)
    if not s["t0_found"]:
        print("no inflight_above_threshold event in this log: nothing to time from")
        return 1
    print("seconds from in-flight requests first reaching the threshold "
          "(poll resolution applies):")
    for k, v in s["intervals_s"].items():
        print(f"  {k:<28} {'not observed' if v is None else v}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--out", required=True)
    w.add_argument("--namespace", default="vllm")
    w.add_argument("--deployment", default="vllm")
    w.add_argument("--pod-selector", default="app=vllm")
    w.add_argument("--node-selector", default="doks.digitalocean.com/gpu-brand=amd")
    w.add_argument("--prometheus-service", default="prometheus:9090",
                   help="service:port in the namespace, read through the API server proxy; '' disables")
    w.add_argument("--query", default=INFLIGHT_QUERY)
    w.add_argument("--inflight-threshold", type=float, required=True,
                   help="the autoscaling target per replica (TARGET_INFLIGHT in the ScaledObject)")
    w.add_argument("--interval", type=float, default=3)
    w.add_argument("--max-minutes", type=float, default=60)
    w.set_defaults(fn=cmd_watch)
    s = sub.add_parser("summary")
    s.add_argument("events")
    s.set_defaults(fn=cmd_summary)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
