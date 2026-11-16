#!/usr/bin/env python3
"""FAKE OpenAI-compatible streaming server. For tests only. It is not a model.

It answers /v1/chat/completions with a stream of placeholder tokens on fixed timers, so that
loadgen.py can be tested end to end without a GPU. Every response carries the header
`X-Fake-OpenAI-Server: 1` and a `system_fingerprint` that starts with "fake", and loadgen.py
marks any result produced against it as fake. Numbers measured against this server describe
`time.sleep`, not an inference server. Never publish them as serving numbers.

    python3 serving/fake_openai_server.py --port 8001 --ttft-ms 50 --token-ms 5
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE_HEADER = "X-Fake-OpenAI-Server"
FAKE_FINGERPRINT = "fake-not-a-gpu"
FAKE_MODEL = "fake-model"
PROMPT_TOKENS = 7  # constant, so tests can check totals by hand


class FakeState:
    def __init__(self, ttft_s=0.02, token_s=0.002, fail_every=0, drop_every=0):
        self.ttft_s = ttft_s
        self.token_s = token_s
        self.fail_every = fail_every      # every Nth request gets HTTP 503 (0 = never)
        self.drop_every = drop_every      # every Nth request has its stream cut (0 = never)
        self.lock = threading.Lock()
        self.requests = 0

    def next_request(self) -> int:
        with self.lock:
            self.requests += 1
            return self.requests


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeState  # set by make_server

    def log_message(self, *args):  # keep test output quiet
        pass

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(FAKE_HEADER, "1")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "fake": True})
        elif self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": FAKE_MODEL, "object": "model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        st = self.state
        n = st.next_request()
        if st.fail_every and n % st.fail_every == 0:
            self._json(503, {"error": "fake overload"})
            return
        tokens = max(1, int(body.get("max_tokens") or 16))
        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        drop = bool(st.drop_every and n % st.drop_every == 0)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header(FAKE_HEADER, "1")
        self.send_header("Connection", "close")
        self.end_headers()

        def send(obj):
            self.wfile.write(b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n")
            self.wfile.flush()

        base = {"id": f"fake-{n}", "object": "chat.completion.chunk", "model": FAKE_MODEL,
                "system_fingerprint": FAKE_FINGERPRINT}
        try:
            send({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]})
            time.sleep(st.ttft_s)
            for i in range(tokens):
                if drop and i == tokens // 2:
                    return  # connection closes with no finish_reason and no [DONE]
                if i:
                    time.sleep(st.token_s)
                send({**base, "choices": [{"index": 0, "delta": {"content": "tok "}}]})
            send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
            if want_usage:
                send({**base, "choices": [], "usage": {
                    "prompt_tokens": PROMPT_TOKENS, "completion_tokens": tokens,
                    "total_tokens": PROMPT_TOKENS + tokens}})
            send(b"[DONE]")
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_server(port=0, host="127.0.0.1", **kw) -> ThreadingHTTPServer:
    """Build the fake server (port 0 picks a free port). Caller runs serve_forever()."""
    handler = type("BoundHandler", (Handler,), {"state": FakeState(**kw)})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--ttft-ms", type=float, default=20)
    ap.add_argument("--token-ms", type=float, default=2)
    ap.add_argument("--fail-every", type=int, default=0)
    ap.add_argument("--drop-every", type=int, default=0)
    a = ap.parse_args()
    srv = make_server(a.port, a.host, ttft_s=a.ttft_ms / 1000, token_s=a.token_ms / 1000,
                      fail_every=a.fail_every, drop_every=a.drop_every)
    print(f"FAKE OpenAI-compatible server on http://{a.host}:{srv.server_address[1]} "
          f"(tests only, not a model)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
