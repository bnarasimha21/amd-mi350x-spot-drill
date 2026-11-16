#!/usr/bin/env python3
"""Load generator for an OpenAI-compatible /v1/chat/completions endpoint, streaming.

Standard library only, so it runs unchanged on a laptop, in CI, and in a bare python image
inside the cluster. For each concurrency level it keeps N requests in flight for a fixed
duration and records, per request and in aggregate:

  time to first token   request sent -> first streamed delta that carries text
  inter-chunk latency   gap between successive text deltas (pooled over all requests)
  time per token        (last text delta - first text delta) / (completion tokens - 1)
  output tokens/s       per request (completion tokens / request duration) and for the level
                        (all completion tokens / wall time of the level)
  errors                by kind, plus how many requests needed a retry

The load is closed-loop: each worker sends its next request when the previous one ends, and
waits --error-backoff after a failure. Failure counts are therefore requests that failed,
not requests that would have arrived from independent users during an outage.

Token counts come from the server's `usage` block (`stream_options.include_usage`). If the
server sends none, text deltas are counted instead and the result says so
(`token_count_source: "chunks"`), because one delta is not always one token.

The result file carries the full configuration. If the target identifies itself as the fake
test server (serving/fake_openai_server.py) the result is marked `is_fake_server: true` and
`publishable: false`. Those numbers time a sleep loop. They are not serving numbers.

    python3 serving/loadgen.py --base-url http://vllm:8000 --model qwen3-8b \
        --concurrency 1,2,4,8,16,32 --duration 120 --max-tokens 256 \
        --hardware "1x MI350X spot, DOKS" --out results/serving/sweep.json
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import signal
import socket
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit

SCHEMA = "spot-drill-serving-loadgen/1"
FAKE_HEADER = "X-Fake-OpenAI-Server"
FAKE_DISCLAIMER = ("FAKE SERVER. These numbers time serving/fake_openai_server.py, a sleep loop "
                   "used for tests. They are not serving numbers and must not be published.")

# A small fixed prompt set. Identical prompts let a server reuse its prefix cache, so the
# set is cycled per request. Use --prompt-file for a workload that looks like yours.
DEFAULT_PROMPTS = [
    "Explain what a checkpoint is in model training, in one paragraph.",
    "Write a short description of how a hash table handles collisions.",
    "Summarise the trade-offs between spot and on-demand compute.",
    "Describe how TCP slow start works.",
    "Give three reasons a database index can make a query slower.",
    "Explain the difference between a process and a thread.",
    "Describe what a readiness probe is for in Kubernetes.",
    "Explain why floating point addition is not associative.",
]


def percentile(values, p):
    """p-th percentile (0..100) with linear interpolation between closest ranks.

    Same definition as numpy's default: rank = p/100 * (n-1). percentile([1,2,3,4], 50) = 2.5.
    Returns None for an empty list.
    """
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    rank = (p / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = min(lo + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (rank - lo))


def summarise(values):
    """count, mean, p50, p95, p99, min, max. None fields when there are no values."""
    vals = [v for v in values if v is not None]
    if not vals:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None,
                "min": None, "max": None}
    return {"count": len(vals), "mean": sum(vals) / len(vals),
            "p50": percentile(vals, 50), "p95": percentile(vals, 95),
            "p99": percentile(vals, 99), "min": float(min(vals)), "max": float(max(vals))}


class Target:
    def __init__(self, base_url: str, api_key: str | None, timeout: float):
        u = urlsplit(base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"--base-url must be http(s)://host[:port], got {base_url!r}")
        self.scheme, self.host = u.scheme, u.hostname
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.prefix = u.path.rstrip("/")
        self.api_key, self.timeout = api_key, timeout

    def connect(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.timeout)

    def headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h


def probe(target: Target) -> dict:
    """GET /v1/models once: is the endpoint there, and does it say it is the fake server?"""
    info = {"reachable": False, "is_fake_server": False, "models": []}
    try:
        c = target.connect()
        c.request("GET", target.prefix + "/v1/models", headers=target.headers())
        r = c.getresponse()
        body = r.read()
        info["reachable"] = r.status == 200
        info["http_status"] = r.status
        info["is_fake_server"] = r.getheader(FAKE_HEADER) is not None
        if r.status == 200:
            info["models"] = [m.get("id") for m in json.loads(body).get("data", [])]
        c.close()
    except (OSError, ValueError, http.client.HTTPException) as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def one_attempt(target: Target, payload: dict) -> dict:
    """Send one streaming request. Never raises: failures come back as ok=False."""
    rec = {"ok": False, "error": None, "http_status": None, "ttft_s": None, "duration_s": None,
           "completion_tokens": None, "prompt_tokens": None, "token_count_source": None,
           "text_chunks": 0, "gaps_s": [], "decode_s": None, "saw_fake": False}
    t0 = time.perf_counter()
    conn = None
    try:
        conn = target.connect()
        conn.request("POST", target.prefix + "/v1/chat/completions",
                     body=json.dumps(payload).encode(), headers=target.headers())
        resp = conn.getresponse()
        rec["http_status"] = resp.status
        if resp.getheader(FAKE_HEADER) is not None:
            rec["saw_fake"] = True
        if resp.status != 200:
            resp.read()
            rec["error"] = f"http_{resp.status}"
            return rec
        first = last = None
        usage, finished, done = None, False, False
        for raw in resp:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                done = True
                break
            now = time.perf_counter()
            try:
                chunk = json.loads(data)
            except ValueError:
                rec["error"] = "bad_json"
                return rec
            if str(chunk.get("system_fingerprint") or "").startswith("fake"):
                rec["saw_fake"] = True
            if chunk.get("error"):
                rec["error"] = "stream_error"
                return rec
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                # Reasoning models stream thinking text in a separate field. It is output
                # the GPU produced, so it counts as a token for latency purposes.
                if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                    if first is None:
                        first = now
                    else:
                        rec["gaps_s"].append(now - last)
                    last = now
                    rec["text_chunks"] += 1
                if choice.get("finish_reason"):
                    finished = True
        t_end = time.perf_counter()
        if not (finished or done):
            rec["error"] = "stream_truncated"  # connection closed mid-answer
            return rec
        if first is None:
            rec["error"] = "no_tokens"
            return rec
        rec["ttft_s"] = first - t0
        rec["duration_s"] = t_end - t0
        rec["decode_s"] = last - first
        if usage and usage.get("completion_tokens") is not None:
            rec["completion_tokens"] = int(usage["completion_tokens"])
            rec["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
            rec["token_count_source"] = "usage"
        else:
            rec["completion_tokens"] = rec["text_chunks"]
            rec["token_count_source"] = "chunks"
        rec["ok"] = True
        return rec
    except socket.timeout:
        rec["error"] = "timeout"
    except ConnectionRefusedError:
        rec["error"] = "connection_refused"
    except (ConnectionError, http.client.HTTPException) as e:
        rec["error"] = "connection_" + type(e).__name__
    except OSError as e:
        rec["error"] = "os_" + type(e).__name__
    finally:
        if conn is not None:
            conn.close()
        if rec["duration_s"] is None:
            rec["duration_s"] = time.perf_counter() - t0
    return rec


def run_level(target: Target, cfg: dict, concurrency: int, run_t0: float) -> tuple[dict, list]:
    """Hold `concurrency` requests in flight for cfg['duration_s']. Returns (summary, records)."""
    prompts, records, lock = cfg["prompts"], [], threading.Lock()
    counter = [0]
    level_t0 = time.perf_counter()
    deadline = level_t0 + cfg["duration_s"]
    stop = cfg["stop_event"]

    def worker(wid: int):
        while time.perf_counter() < deadline and not stop.is_set():
            with lock:
                seq = counter[0]
                counter[0] += 1
            payload = {"model": cfg["model"], "stream": True,
                       "stream_options": {"include_usage": True},
                       "max_tokens": cfg["max_tokens"], "temperature": cfg["temperature"],
                       "messages": [{"role": "user", "content": prompts[seq % len(prompts)]}]}
            payload.update(cfg["extra_body"])
            started = time.perf_counter()
            attempts, errors = 0, []
            while True:
                attempts += 1
                rec = one_attempt(target, payload)
                if rec["ok"] or attempts > cfg["retries"] or stop.is_set():
                    break
                errors.append(rec["error"])
                time.sleep(cfg["retry_backoff_s"])
            if not rec["ok"] and cfg["error_backoff_s"] > 0:
                # Without this a dead endpoint turns each worker into a tight loop and the
                # failure count measures the loop, not the outage.
                stop.wait(cfg["error_backoff_s"])
            rec.update(concurrency=concurrency, worker=wid, seq=seq, attempts=attempts,
                       retry_errors=errors,
                       start_s=round(started - run_t0, 4),
                       end_s=round(time.perf_counter() - run_t0, 4))
            with lock:
                records.append(rec)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(concurrency)]
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(timeout=0.2)
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join(timeout=cfg["timeout_s"] + 1)
    wall = time.perf_counter() - level_t0
    return summarise_level(records, concurrency, wall, cfg["timeline_bucket_s"],
                           level_t0 - run_t0), records


def summarise_level(records: list, concurrency: int, wall_s: float, bucket_s: float,
                    level_start_s: float) -> dict:
    ok = [r for r in records if r["ok"]]
    bad = [r for r in records if not r["ok"]]
    out_tok = sum(r["completion_tokens"] for r in ok)
    in_tok = sum(r["prompt_tokens"] or 0 for r in ok)
    errors = Counter(r["error"] for r in bad)
    retry_errors = Counter(e for r in records for e in r["retry_errors"])
    gaps = [g for r in ok for g in r["gaps_s"]]
    per_tok = [r["decode_s"] / (r["completion_tokens"] - 1) for r in ok if r["completion_tokens"] > 1]
    sources = sorted({r["token_count_source"] for r in ok})

    timeline = []
    if bucket_s > 0 and records:
        n = int(max(r["end_s"] - level_start_s for r in records) // bucket_s) + 1
        buckets = [{"t_s": round(level_start_s + i * bucket_s, 3), "ok": 0, "failed": 0,
                    "retried": 0, "output_tokens": 0} for i in range(n)]
        for r in records:  # a request lands in the bucket in which it finished
            b = buckets[min(n - 1, max(0, int((r["end_s"] - level_start_s) // bucket_s)))]
            b["ok" if r["ok"] else "failed"] += 1
            b["retried"] += 1 if r["attempts"] > 1 else 0
            b["output_tokens"] += (r["completion_tokens"] or 0) if r["ok"] else 0
        timeline = buckets

    return {
        "concurrency": concurrency,
        "wall_s": wall_s,
        "requests_total": len(records),
        "requests_ok": len(ok),
        "requests_failed": len(bad),
        "requests_retried": sum(1 for r in records if r["attempts"] > 1),
        "requests_ok_after_retry": sum(1 for r in ok if r["attempts"] > 1),
        "errors": dict(errors),
        "retry_errors": dict(retry_errors),
        "output_tokens": out_tok,
        "prompt_tokens": in_tok,
        "total_tokens": out_tok + in_tok,
        "token_count_source": sources[0] if len(sources) == 1 else (sources or None),
        "output_tokens_per_s": out_tok / wall_s if wall_s > 0 else None,
        "total_tokens_per_s": (out_tok + in_tok) / wall_s if wall_s > 0 else None,
        "requests_per_s": len(ok) / wall_s if wall_s > 0 else None,
        "ttft_s": summarise([r["ttft_s"] for r in ok]),
        "inter_chunk_s": summarise(gaps),
        "time_per_output_token_s": summarise(per_tok),
        "request_output_tokens_per_s": summarise(
            [r["completion_tokens"] / r["duration_s"] for r in ok if r["duration_s"] > 0]),
        "request_duration_s": summarise([r["duration_s"] for r in ok]),
        "timeline": timeline,
    }


def build_result(args_cfg: dict, probe_info: dict, levels: list, records: list,
                 interrupted: bool, started_at: str) -> dict:
    fake = bool(probe_info.get("is_fake_server") or any(r.get("saw_fake") for r in records))
    result = {
        "schema": SCHEMA,
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "is_fake_server": fake,
        "publishable": not fake,
        "interrupted": interrupted,
        "config": args_cfg,
        "target": probe_info,
        "levels": levels,
    }
    if fake:
        result["disclaimer"] = FAKE_DISCLAIMER
    if args_cfg.get("save_requests"):
        result["requests"] = [{k: v for k, v in r.items() if k != "gaps_s"} for r in records]
    return result


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="http(s)://host:port, without /v1")
    ap.add_argument("--model", required=True, help="model name the server was started with")
    ap.add_argument("--concurrency", default="1,2,4,8", help="comma-separated levels, run in order")
    ap.add_argument("--duration", type=float, default=60, help="seconds per level")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=120, help="socket timeout per request, seconds")
    ap.add_argument("--retries", type=int, default=0, help="extra attempts after a failed request")
    ap.add_argument("--retry-backoff", type=float, default=0.5, help="seconds between attempts")
    ap.add_argument("--error-backoff", type=float, default=0.25,
                    help="seconds a worker waits after a request that finally failed")
    ap.add_argument("--pause", type=float, default=5, help="idle seconds between levels")
    ap.add_argument("--prompt-file", help="one prompt per line; default is a small built-in set")
    ap.add_argument("--extra-body", default="{}",
                    help='JSON merged into every request, e.g. \'{"chat_template_kwargs":{"enable_thinking":false}}\'')
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"),
                    help="bearer token; default $OPENAI_API_KEY. Never written to the result")
    ap.add_argument("--hardware", default="unspecified",
                    help="free text recorded in the result: GPU, count, spot or on-demand, where")
    ap.add_argument("--note", default="", help="free text recorded in the result")
    ap.add_argument("--timeline-bucket", type=float, default=10, help="seconds; 0 disables")
    ap.add_argument("--save-requests", action="store_true", help="include every request record")
    ap.add_argument("--out", required=True, help="result JSON path")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    levels_wanted = [int(x) for x in a.concurrency.split(",") if x.strip()]
    if not levels_wanted or min(levels_wanted) < 1:
        sys.exit("--concurrency needs positive integers, e.g. 1,2,4,8")
    prompts = DEFAULT_PROMPTS
    if a.prompt_file:
        prompts = [l.strip() for l in open(a.prompt_file, encoding="utf-8") if l.strip()]
        if not prompts:
            sys.exit(f"{a.prompt_file} has no prompts")
    try:
        extra = json.loads(a.extra_body)
        assert isinstance(extra, dict)
    except (ValueError, AssertionError):
        sys.exit("--extra-body must be a JSON object")

    target = Target(a.base_url, a.api_key, a.timeout)
    public_cfg = {
        "base_url": a.base_url, "model": a.model, "concurrency_levels": levels_wanted,
        "duration_s": a.duration, "max_tokens": a.max_tokens, "temperature": a.temperature,
        "timeout_s": a.timeout, "retries": a.retries, "retry_backoff_s": a.retry_backoff,
        "error_backoff_s": a.error_backoff,
        "pause_s": a.pause, "prompt_source": a.prompt_file or "built-in",
        "prompt_count": len(prompts), "extra_body": extra, "api_key_set": bool(a.api_key),
        "hardware": a.hardware, "note": a.note, "timeline_bucket_s": a.timeline_bucket,
        "save_requests": a.save_requests, "argv": sys.argv[1:] if argv is None else list(argv),
        "host": socket.gethostname(), "python": sys.version.split()[0],
    }
    if "--api-key" in public_cfg["argv"]:  # the key itself never goes into the file
        i = public_cfg["argv"].index("--api-key")
        public_cfg["argv"][i + 1:i + 2] = ["REDACTED"]
    public_cfg["argv"] = ["--api-key=REDACTED" if x.startswith("--api-key=") else x
                          for x in public_cfg["argv"]]
    run_cfg = {**public_cfg, "prompts": prompts, "stop_event": threading.Event()}

    # SIGTERM or SIGINT: finish the requests in flight, write what there is, mark it
    # interrupted. Set explicitly because a process started in the background by a
    # non-interactive shell inherits SIGINT as ignored.
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: run_cfg["stop_event"].set())

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    info = probe(target)
    if info["is_fake_server"]:
        print("!!! " + FAKE_DISCLAIMER, file=sys.stderr)
    if not info["reachable"]:
        print(f"WARN: GET /v1/models did not return 200 ({info}); continuing anyway", file=sys.stderr)

    run_t0 = time.perf_counter()
    levels, records, interrupted = [], [], False
    for i, c in enumerate(levels_wanted):
        if i and a.pause > 0:
            time.sleep(a.pause)
        print(f"[loadgen] concurrency {c} for {a.duration:.0f}s ...", file=sys.stderr, flush=True)
        summary, recs = run_level(target, run_cfg, c, run_t0)
        levels.append(summary)
        records.extend(recs)
        tps = summary["output_tokens_per_s"] or 0
        ttft = summary["ttft_s"]["p50"]
        print(f"[loadgen]   ok={summary['requests_ok']} failed={summary['requests_failed']} "
              f"retried={summary['requests_retried']} out_tok/s={tps:.1f} "
              f"ttft_p50={'n/a' if ttft is None else f'{ttft * 1000:.0f}ms'}",
              file=sys.stderr, flush=True)
        if run_cfg["stop_event"].is_set():
            interrupted = True
            break

    result = build_result(public_cfg, info, levels, records, interrupted, started_at)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    print(f"[loadgen] wrote {a.out}" + ("  (FAKE SERVER, not serving numbers)" if result["is_fake_server"] else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
