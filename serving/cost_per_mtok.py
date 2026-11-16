#!/usr/bin/env python3
"""Cost per million tokens from a load-test result and an hourly price.

The arithmetic, for one concurrency level:

    tokens per hour      = tokens per second (measured by loadgen.py) * 3600
    cost per 1M tokens   = hourly cost / tokens per hour * 1,000,000

where hourly cost = GPU nodes * price per node + any overhead you pass (the CPU pool, for
example). That is the cost when the GPUs are kept as busy as they were during the test,
which is 100% utilisation of that level. Real traffic is not flat, so the tool also prints:

    cost at utilisation u       = cost at 100% / u
    utilisation to hit target P = cost at 100% / P      (above 1.0: not reachable at this level)

Utilisation here means the fraction of paid GPU time spent serving at the measured rate.

The price is passed with --price-hourly or looked up live with --size-slug (see pricing.py).
Nothing is assumed. A result produced against the fake test server is refused unless
--allow-fake is given, and then every line of output says FAKE.

    python3 serving/cost_per_mtok.py results/serving/sweep.json \
        --price-hourly "$(cat .state/serving-gpu-price-hourly)" \
        --price-source "locked at pool creation" --gpu-nodes 1 --target-per-mtok "$TARGET_USD"
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pricing import PriceError, resolve_price  # noqa: E402

DEFAULT_UTILISATIONS = (1.0, 0.75, 0.5, 0.25)


def cost_per_million(hourly_cost: float, tokens_per_s: float | None) -> float | None:
    """Dollars per million tokens at a steady tokens_per_s. None when nothing was produced."""
    if not tokens_per_s or tokens_per_s <= 0:
        return None
    return hourly_cost / (tokens_per_s * 3600.0) * 1_000_000.0


def at_utilisation(cost_full: float | None, utilisation: float) -> float | None:
    if cost_full is None:
        return None
    if not 0 < utilisation <= 1:
        raise ValueError("utilisation must be in (0, 1]")
    return cost_full / utilisation


def utilisation_for_target(cost_full: float | None, target: float) -> float | None:
    """Fraction of paid time that must be spent at the measured rate to hit the target price."""
    if cost_full is None:
        return None
    if target <= 0:
        raise ValueError("target price must be positive")
    return cost_full / target


def analyse(result: dict, hourly_cost: float, utilisations=DEFAULT_UTILISATIONS,
            target: float | None = None) -> list[dict]:
    rows = []
    for lv in result["levels"]:
        out_full = cost_per_million(hourly_cost, lv.get("output_tokens_per_s"))
        tot_full = cost_per_million(hourly_cost, lv.get("total_tokens_per_s"))
        row = {
            "concurrency": lv["concurrency"],
            "output_tokens_per_s": lv.get("output_tokens_per_s"),
            "total_tokens_per_s": lv.get("total_tokens_per_s"),
            "requests_failed": lv.get("requests_failed"),
            "usd_per_mtok_output": out_full,
            "usd_per_mtok_total": tot_full,
            "at_utilisation": [
                {"utilisation": u, "usd_per_mtok_output": at_utilisation(out_full, u),
                 "usd_per_mtok_total": at_utilisation(tot_full, u)} for u in utilisations],
        }
        if target is not None:
            for key, full in (("output", out_full), ("total", tot_full)):
                u = utilisation_for_target(full, target)
                row[f"utilisation_for_target_{key}"] = u
                row[f"target_reachable_{key}"] = None if u is None else u <= 1.0
        rows.append(row)
    return rows


def fmt(x, spec=".4f"):
    return "n/a" if x is None else format(x, spec)


def render(report: dict) -> str:
    tag = "FAKE INPUT, NOT SERVING NUMBERS | " if report["is_fake_server"] else ""
    lines = [
        f"{tag}input: {report['input']}",
        f"{tag}hardware (as recorded by the operator): {report['hardware']}",
        f"{tag}hourly cost: ${report['hourly_cost']:.4f} = {report['gpu_nodes']} GPU node(s) x "
        f"${report['price_hourly_per_node']:.4f} + ${report['overhead_hourly']:.4f} overhead",
        f"{tag}price source: {report['price_source']}",
        "",
        f"{tag}{'conc':>5} {'out tok/s':>10} {'tot tok/s':>10} {'$/M out':>10} {'$/M total':>10} {'failed':>7}",
    ]
    for r in report["levels"]:
        lines.append(f"{tag}{r['concurrency']:>5} {fmt(r['output_tokens_per_s'], '.1f'):>10} "
                     f"{fmt(r['total_tokens_per_s'], '.1f'):>10} {fmt(r['usd_per_mtok_output']):>10} "
                     f"{fmt(r['usd_per_mtok_total']):>10} {r['requests_failed']!s:>7}")
    lines += ["", f"{tag}$/M output tokens by utilisation (fraction of paid GPU time at the measured rate):"]
    us = [u["utilisation"] for u in report["levels"][0]["at_utilisation"]] if report["levels"] else []
    lines.append(f"{tag}{'conc':>5} " + " ".join(f"{f'{u:.0%}':>10}" for u in us))
    for r in report["levels"]:
        lines.append(f"{tag}{r['concurrency']:>5} " + " ".join(
            f"{fmt(u['usd_per_mtok_output']):>10}" for u in r["at_utilisation"]))
    if report.get("target_per_mtok") is not None:
        lines += ["", f"{tag}utilisation needed for ${report['target_per_mtok']:.4f} per 1M tokens:"]
        for r in report["levels"]:
            parts = []
            for key in ("output", "total"):
                u = r[f"utilisation_for_target_{key}"]
                parts.append(f"{key}: " + ("n/a" if u is None else
                             f"{u:.1%}" + ("" if u <= 1 else " (not reachable at this level)")))
            lines.append(f"{tag}{r['concurrency']:>5}  " + "   ".join(parts))
    lines += ["", f"{tag}Cost at 100% assumes the GPUs stay as loaded as they were in the test. "
              "Levels with failed requests overstate usable throughput."]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result", help="JSON written by serving/loadgen.py")
    ap.add_argument("--price-hourly", type=float, help="USD per hour for ONE GPU node")
    ap.add_argument("--price-source", help="where --price-hourly came from; recorded in the output")
    ap.add_argument("--size-slug", help="look the price up live with doctl instead")
    ap.add_argument("--gpu-nodes", type=int, default=1, help="GPU nodes serving during the test")
    ap.add_argument("--overhead-hourly", type=float, default=0.0,
                    help="other hourly cost to include, e.g. the CPU node pool")
    ap.add_argument("--utilisation", default=",".join(str(u) for u in DEFAULT_UTILISATIONS))
    ap.add_argument("--target-per-mtok", type=float, help="target USD per 1M tokens")
    ap.add_argument("--allow-fake", action="store_true",
                    help="accept a result from the fake test server (output is marked FAKE)")
    ap.add_argument("--out", help="also write the report as JSON")
    a = ap.parse_args(argv)

    with open(a.result, encoding="utf-8") as f:
        result = json.load(f)
    fake = bool(result.get("is_fake_server"))
    if fake and not a.allow_fake:
        sys.exit("REFUSING: this result came from the fake test server. It is not a serving "
                 "measurement, so a cost per token from it would be fiction. "
                 "(--allow-fake to exercise the arithmetic; output is then marked FAKE.)")
    if a.gpu_nodes < 1 or a.overhead_hourly < 0:
        sys.exit("--gpu-nodes must be >= 1 and --overhead-hourly >= 0")
    try:
        price, source = resolve_price(a.price_hourly, a.size_slug, a.price_source)
    except PriceError as e:
        sys.exit(f"no price: {e}")
    utilisations = tuple(float(x) for x in a.utilisation.split(",") if x.strip())
    hourly = a.gpu_nodes * price + a.overhead_hourly
    report = {
        "schema": "spot-drill-serving-cost/1",
        "input": a.result,
        "is_fake_server": fake,
        "publishable": bool(result.get("publishable")) and not fake,
        "hardware": (result.get("config") or {}).get("hardware", "unspecified"),
        "price_hourly_per_node": price,
        "price_source": source,
        "gpu_nodes": a.gpu_nodes,
        "overhead_hourly": a.overhead_hourly,
        "hourly_cost": hourly,
        "target_per_mtok": a.target_per_mtok,
        "levels": analyse(result, hourly, utilisations, a.target_per_mtok),
    }
    if fake:
        report["disclaimer"] = result.get("disclaimer", "FAKE SERVER INPUT")
    print(render(report))
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
