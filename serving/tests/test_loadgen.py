"""loadgen.py end to end against the FAKE server. These are tests of the tool, not
measurements of anything."""
import contextlib
import io
import json
import os
import socket
import tempfile
import unittest

from ._util import FakeServer
import fake_openai_server
import loadgen


def run(url, *extra):
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "r.json")
        with contextlib.redirect_stderr(io.StringIO()):   # progress lines, not needed here
            rc = loadgen.main(["--base-url", url, "--model", "fake-model", "--out", out,
                               "--pause", "0", *extra])
        assert rc == 0
        with open(out) as f:
            return json.load(f)


class Percentiles(unittest.TestCase):
    def test_interpolation_by_hand(self):
        self.assertEqual(loadgen.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(loadgen.percentile([4, 1, 3, 2], 50), 2.5)       # order does not matter
        self.assertEqual(loadgen.percentile([10, 20, 30, 40, 50], 0), 10)
        self.assertEqual(loadgen.percentile([10, 20, 30, 40, 50], 100), 50)
        self.assertEqual(loadgen.percentile([10, 20, 30, 40, 50], 25), 20)
        # 101 values 0..100: the p-th percentile is p
        self.assertAlmostEqual(loadgen.percentile(list(range(101)), 95), 95)
        self.assertAlmostEqual(loadgen.percentile(list(range(101)), 99), 99)
        # rank = 0.95 * 9 = 8.55 -> 9 + 0.55 * (10 - 9)
        self.assertAlmostEqual(loadgen.percentile(list(range(1, 11)), 95), 9.55)

    def test_edges(self):
        self.assertIsNone(loadgen.percentile([], 50))
        self.assertEqual(loadgen.percentile([7], 99), 7.0)
        s = loadgen.summarise([])
        self.assertEqual(s["count"], 0)
        self.assertIsNone(s["p95"])
        s = loadgen.summarise([1.0, None, 3.0])
        self.assertEqual((s["count"], s["mean"], s["min"], s["max"]), (2, 2.0, 1.0, 3.0))


class LevelSummary(unittest.TestCase):
    def rec(self, ok=True, tokens=10, dur=2.0, end=1.0, attempts=1, error=None):
        return {"ok": ok, "error": error, "completion_tokens": tokens if ok else None,
                "prompt_tokens": 5 if ok else None, "token_count_source": "usage" if ok else None,
                "ttft_s": 0.5 if ok else None, "duration_s": dur, "decode_s": 0.9 if ok else None,
                "gaps_s": [0.1] * (tokens - 1) if ok else [], "attempts": attempts,
                "retry_errors": ["http_503"] * (attempts - 1), "end_s": end}

    def test_arithmetic_by_hand(self):
        recs = [self.rec(end=1), self.rec(end=12), self.rec(ok=False, error="timeout", end=13),
                self.rec(attempts=2, end=14)]
        s = loadgen.summarise_level(recs, 2, wall_s=20.0, bucket_s=10, level_start_s=0.0)
        self.assertEqual((s["requests_total"], s["requests_ok"], s["requests_failed"]), (4, 3, 1))
        self.assertEqual(s["requests_retried"], 1)
        self.assertEqual(s["requests_ok_after_retry"], 1)
        self.assertEqual(s["errors"], {"timeout": 1})
        self.assertEqual(s["retry_errors"], {"http_503": 1})
        self.assertEqual(s["output_tokens"], 30)            # 3 ok x 10
        self.assertEqual(s["prompt_tokens"], 15)            # 3 ok x 5
        self.assertEqual(s["output_tokens_per_s"], 1.5)     # 30 / 20 s
        self.assertEqual(s["total_tokens_per_s"], 2.25)     # 45 / 20 s
        self.assertEqual(s["request_output_tokens_per_s"]["p50"], 5.0)   # 10 tokens / 2 s
        self.assertAlmostEqual(s["time_per_output_token_s"]["p50"], 0.1)  # 0.9 s / 9 gaps
        self.assertEqual(s["inter_chunk_s"]["count"], 27)
        self.assertEqual([(b["ok"], b["failed"], b["output_tokens"]) for b in s["timeline"]],
                         [(1, 0, 10), (2, 1, 20)])


class EndToEndAgainstFake(unittest.TestCase):
    def test_counts_tokens_and_marks_result_fake(self):
        with FakeServer(ttft_s=0.03, token_s=0.002) as fs:
            r = run(fs.url, "--concurrency", "1,3", "--duration", "0.6", "--max-tokens", "8",
                    "--hardware", "none: fake server", "--save-requests")
        self.assertEqual(r["schema"], loadgen.SCHEMA)
        self.assertTrue(r["is_fake_server"])
        self.assertFalse(r["publishable"])
        self.assertIn("not serving numbers", r["disclaimer"])
        self.assertTrue(r["target"]["is_fake_server"])
        self.assertEqual(r["target"]["models"], ["fake-model"])
        # the full configuration is in the file
        c = r["config"]
        self.assertEqual(c["concurrency_levels"], [1, 3])
        self.assertEqual((c["duration_s"], c["max_tokens"], c["model"]), (0.6, 8, "fake-model"))
        self.assertEqual(c["hardware"], "none: fake server")
        self.assertEqual(c["base_url"], fs.url)
        self.assertEqual([l["concurrency"] for l in r["levels"]], [1, 3])
        for lv in r["levels"]:
            self.assertGreater(lv["requests_ok"], 0)
            self.assertEqual(lv["requests_failed"], 0)
            self.assertEqual(lv["token_count_source"], "usage")
            # the fake server emits exactly max_tokens tokens and reports 7 prompt tokens
            self.assertEqual(lv["output_tokens"], 8 * lv["requests_ok"])
            self.assertEqual(lv["prompt_tokens"],
                             fake_openai_server.PROMPT_TOKENS * lv["requests_ok"])
            self.assertGreaterEqual(lv["ttft_s"]["min"], 0.03)       # the configured delay
            self.assertGreaterEqual(lv["inter_chunk_s"]["p50"], 0.002)
            self.assertEqual(lv["inter_chunk_s"]["count"], 7 * lv["requests_ok"])
            self.assertAlmostEqual(lv["output_tokens_per_s"], lv["output_tokens"] / lv["wall_s"])
            self.assertGreaterEqual(lv["wall_s"], 0.6)
        # three workers finish more requests than one in the same time
        self.assertGreater(r["levels"][1]["requests_ok"], r["levels"][0]["requests_ok"])
        self.assertEqual(len(r["requests"]),
                         sum(l["requests_total"] for l in r["levels"]))

    def test_http_errors_are_counted_not_hidden(self):
        with FakeServer(ttft_s=0.001, token_s=0.0, fail_every=2) as fs:
            r = run(fs.url, "--concurrency", "1", "--duration", "0.4", "--max-tokens", "2")
        lv = r["levels"][0]
        self.assertGreater(lv["requests_failed"], 0)
        self.assertEqual(lv["errors"], {"http_503": lv["requests_failed"]})
        self.assertEqual(lv["requests_retried"], 0)
        # one worker, every 2nd request fails: ok and failed differ by at most one
        self.assertLessEqual(abs(lv["requests_ok"] - lv["requests_failed"]), 1)
        self.assertEqual(lv["output_tokens"], 2 * lv["requests_ok"])   # failures add no tokens

    def test_retries_recover_and_are_reported(self):
        with FakeServer(ttft_s=0.001, token_s=0.0, fail_every=2) as fs:
            r = run(fs.url, "--concurrency", "1", "--duration", "0.4", "--max-tokens", "2",
                    "--retries", "2", "--retry-backoff", "0")
        lv = r["levels"][0]
        self.assertEqual(lv["requests_failed"], 0)
        self.assertGreater(lv["requests_retried"], 0)
        self.assertEqual(lv["requests_ok_after_retry"], lv["requests_retried"])
        self.assertEqual(lv["retry_errors"], {"http_503": lv["requests_retried"]})

    def test_cut_stream_is_an_error(self):
        with FakeServer(ttft_s=0.001, token_s=0.0, drop_every=1) as fs:
            r = run(fs.url, "--concurrency", "2", "--duration", "0.3", "--max-tokens", "6")
        lv = r["levels"][0]
        self.assertEqual(lv["requests_ok"], 0)
        self.assertEqual(lv["errors"], {"stream_truncated": lv["requests_failed"]})
        self.assertIsNone(lv["ttft_s"]["p50"])
        self.assertEqual(lv["output_tokens_per_s"], 0)

    def test_dead_endpoint_fails_cleanly(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = run(f"http://127.0.0.1:{port}", "--concurrency", "1", "--duration", "0.2",
                "--timeout", "1")
        self.assertFalse(r["target"]["reachable"])
        self.assertFalse(r["is_fake_server"])
        lv = r["levels"][0]
        self.assertEqual(lv["requests_ok"], 0)
        self.assertEqual(list(lv["errors"]), ["connection_refused"])

    def test_api_key_never_written(self):
        with FakeServer(ttft_s=0.001, token_s=0.0) as fs:
            r = run(fs.url, "--concurrency", "1", "--duration", "0.1", "--api-key", "sk-secret-123")
        self.assertNotIn("sk-secret-123", json.dumps(r))
        self.assertTrue(r["config"]["api_key_set"])

    def test_chunk_count_fallback_is_labelled(self):
        # usage block withheld: tokens are counted from text deltas and the result says so
        with FakeServer(ttft_s=0.001, token_s=0.0) as fs:
            t = loadgen.Target(fs.url, None, 5)
            rec = loadgen.one_attempt(t, {"model": "m", "stream": True, "max_tokens": 5,
                                          "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["token_count_source"], "chunks")
        self.assertEqual(rec["completion_tokens"], 5)
        self.assertTrue(rec["saw_fake"])


if __name__ == "__main__":
    unittest.main()


class Interrupt(unittest.TestCase):
    def test_sigterm_writes_a_partial_result_marked_interrupted(self):
        import signal
        import subprocess
        import sys
        import time
        from ._util import SERVING
        with FakeServer(ttft_s=0.01, token_s=0.001) as fs, tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.json")
            p = subprocess.Popen([sys.executable, os.path.join(SERVING, "loadgen.py"),
                                  "--base-url", fs.url, "--model", "fake-model",
                                  "--concurrency", "2,4", "--duration", "60", "--max-tokens", "4",
                                  "--out", out], stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=20), 0)
            with open(out) as f:
                r = json.load(f)
        self.assertTrue(r["interrupted"])
        self.assertEqual(len(r["levels"]), 1)            # stopped during the first level
        self.assertLess(r["levels"][0]["wall_s"], 30)
        self.assertGreater(r["levels"][0]["requests_ok"], 0)


if __name__ == "__main__":
    unittest.main()
