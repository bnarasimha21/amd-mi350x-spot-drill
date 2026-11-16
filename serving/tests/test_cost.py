"""Cost arithmetic, with numbers chosen so every expected value can be checked by hand."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from . import _util  # noqa: F401  (puts serving/ on sys.path)
import budget
import cost_per_mtok as cost
import pricing


def result(fake=False, levels=None):
    return {"schema": "spot-drill-serving-loadgen/1", "is_fake_server": fake,
            "publishable": not fake, "config": {"hardware": "test fixture, not a measurement"},
            "levels": levels or [
                {"concurrency": 1, "output_tokens_per_s": 100.0, "total_tokens_per_s": 125.0,
                 "requests_failed": 0},
                {"concurrency": 8, "output_tokens_per_s": 1000.0, "total_tokens_per_s": 2000.0,
                 "requests_failed": 0}]}


class Arithmetic(unittest.TestCase):
    def test_cost_per_million(self):
        # $3.60/hour at 1000 tokens/s: 3,600,000 tokens/hour -> $1.00 per million
        self.assertAlmostEqual(cost.cost_per_million(3.60, 1000), 1.0)
        # ten times slower is ten times dearer
        self.assertAlmostEqual(cost.cost_per_million(3.60, 100), 10.0)
        # $7.20/hour at 500 tokens/s: 1,800,000 tokens/hour -> $4.00 per million
        self.assertAlmostEqual(cost.cost_per_million(7.20, 500), 4.0)
        self.assertIsNone(cost.cost_per_million(3.60, 0))
        self.assertIsNone(cost.cost_per_million(3.60, None))

    def test_utilisation(self):
        self.assertAlmostEqual(cost.at_utilisation(1.0, 0.5), 2.0)    # half idle: double
        self.assertAlmostEqual(cost.at_utilisation(1.0, 0.25), 4.0)
        self.assertAlmostEqual(cost.at_utilisation(1.0, 1.0), 1.0)
        self.assertIsNone(cost.at_utilisation(None, 0.5))
        for bad in (0, -0.1, 1.01):
            with self.assertRaises(ValueError):
                cost.at_utilisation(1.0, bad)

    def test_utilisation_for_target(self):
        # $1.00 at full load, target $2.00: half the paid time must be busy
        self.assertAlmostEqual(cost.utilisation_for_target(1.0, 2.0), 0.5)
        # target below the full-load cost: needs more than 100%, so not reachable
        self.assertAlmostEqual(cost.utilisation_for_target(1.0, 0.8), 1.25)
        self.assertIsNone(cost.utilisation_for_target(None, 1.0))
        with self.assertRaises(ValueError):
            cost.utilisation_for_target(1.0, 0)

    def test_analyse_levels(self):
        rows = cost.analyse(result(), hourly_cost=3.60, utilisations=(1.0, 0.5), target=2.0)
        lo, hi = rows
        self.assertAlmostEqual(lo["usd_per_mtok_output"], 10.0)   # 100 tok/s
        self.assertAlmostEqual(lo["usd_per_mtok_total"], 8.0)     # 125 tok/s
        self.assertAlmostEqual(hi["usd_per_mtok_output"], 1.0)    # 1000 tok/s
        self.assertAlmostEqual(hi["usd_per_mtok_total"], 0.5)     # 2000 tok/s
        self.assertAlmostEqual(hi["at_utilisation"][1]["usd_per_mtok_output"], 2.0)
        self.assertAlmostEqual(hi["utilisation_for_target_output"], 0.5)
        self.assertTrue(hi["target_reachable_output"])
        self.assertAlmostEqual(lo["utilisation_for_target_output"], 5.0)
        self.assertFalse(lo["target_reachable_output"])
        self.assertAlmostEqual(hi["utilisation_for_target_total"], 0.25)

    def test_level_with_no_tokens(self):
        rows = cost.analyse(result(levels=[{"concurrency": 4, "output_tokens_per_s": 0,
                                            "total_tokens_per_s": 0, "requests_failed": 9}]),
                            3.60, target=1.0)
        self.assertIsNone(rows[0]["usd_per_mtok_output"])
        self.assertIsNone(rows[0]["target_reachable_output"])


class Cli(unittest.TestCase):
    def run_cli(self, res, *args):
        with tempfile.TemporaryDirectory() as d:
            path, out = os.path.join(d, "r.json"), os.path.join(d, "c.json")
            with open(path, "w") as f:
                json.dump(res, f)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cost.main([path, "--out", out, *args])
            with open(out) as f:
                return rc, buf.getvalue(), json.load(f)

    def test_two_nodes_plus_overhead(self):
        # 2 x $1.50 + $0.60 overhead = $3.60/hour
        rc, text, rep = self.run_cli(result(), "--price-hourly", "1.50", "--gpu-nodes", "2",
                                     "--overhead-hourly", "0.60", "--price-source", "unit test")
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(rep["hourly_cost"], 3.60)
        self.assertAlmostEqual(rep["levels"][1]["usd_per_mtok_output"], 1.0)
        self.assertEqual(rep["price_source"], "passed by the operator: unit test")
        self.assertTrue(rep["publishable"])
        self.assertNotIn("FAKE", text)

    def test_fake_result_refused(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cli(result(fake=True), "--price-hourly", "3.60")
        self.assertIn("REFUSING", str(e.exception))

    def test_fake_result_allowed_is_marked_on_every_line(self):
        rc, text, rep = self.run_cli(result(fake=True), "--price-hourly", "3.60", "--allow-fake")
        self.assertEqual(rc, 0)
        self.assertTrue(rep["is_fake_server"])
        self.assertFalse(rep["publishable"])
        for line in text.splitlines():
            if line.strip():
                self.assertTrue(line.startswith("FAKE INPUT, NOT SERVING NUMBERS"), line)

    def test_no_price_is_an_error_not_a_guess(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cli(result())
        self.assertIn("no price", str(e.exception))


class Pricing(unittest.TestCase):
    CATALOGUE = json.dumps([{"slug": "size-a", "price_hourly": 1.25},
                            {"slug": "size-b", "price_hourly": 0},
                            {"slug": "size-c"}])

    def test_parse(self):
        self.assertEqual(pricing.parse_size_list(self.CATALOGUE, "size-a"), 1.25)
        for slug in ("size-b", "size-c", "missing"):
            with self.assertRaises(pricing.PriceError):
                pricing.parse_size_list(self.CATALOGUE, slug)
        with self.assertRaises(pricing.PriceError):
            pricing.parse_size_list("not json", "size-a")

    def test_explicit_price_wins_and_is_labelled(self):
        self.assertEqual(pricing.resolve_price(2.5, "ignored", "invoice"),
                         (2.5, "passed by the operator: invoice"))
        with self.assertRaises(pricing.PriceError):
            pricing.resolve_price(None, None)
        with self.assertRaises(pricing.PriceError):
            pricing.resolve_price(-1, None)

    def test_live_lookup_fails_loudly_without_doctl(self):
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = ""
        try:
            with self.assertRaises(pricing.PriceError):
                pricing.fetch_hourly_price("size-a")
        finally:
            os.environ["PATH"] = old


class Budget(unittest.TestCase):
    def test_burn(self):
        # 2 GPU x $4 + 1 CPU x $0.50 + $0.50 other = $9/hour (fixture numbers, not prices)
        self.assertAlmostEqual(budget.burn_per_hour(4.0, 2, 0.5, 1, 0.5), 9.0)

    def test_plan(self):
        p = budget.plan(90.0, 9.0, reserve=0.2)
        self.assertAlmostEqual(p["hours_until_budget_gone"], 10.0)
        self.assertAlmostEqual(p["hours_until_hard_stop"], 8.0)
        self.assertAlmostEqual(p["usd_at_hard_stop"], 72.0)
        for bad in ((0, 9, 0.1), (90, 0, 0.1), (90, 9, 1.0)):
            with self.assertRaises(ValueError):
                budget.plan(*bad)

    def test_cli_says_when_the_plan_does_not_fit(self):
        args = ["--budget", "90", "--gpu-price", "4", "--max-gpu-nodes", "2",
                "--cpu-price", "1", "--cpu-nodes", "1", "--reserve", "0.2"]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(budget.main(args + ["--planned-minutes", "480"]), 0)   # 8 h fits
            self.assertEqual(budget.main(args + ["--planned-minutes", "481"]), 1)
        self.assertIn("DOES NOT FIT", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
