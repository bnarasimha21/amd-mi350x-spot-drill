#!/usr/bin/env python3
"""Turn a budget and live prices into a hard stop time for the paid serving session.

Worst-case burn is every GPU node the pool can reach plus the CPU pool, all running:

    burn per hour  = max GPU nodes * GPU price + CPU nodes * CPU price + other hourly cost
    hours of money = budget / burn per hour
    hard stop      = start + hours of money * (1 - reserve)

The reserve is held back for teardown and for the minutes between deciding to stop and the
last node actually going away. Prices are inputs: pass them, or let the preflight script
look them up with doctl. Nothing here is a quote.

    python3 serving/budget.py --budget 80 --gpu-price "$GPU_PRICE" --max-gpu-nodes 2 \
        --cpu-price "$CPU_PRICE" --cpu-nodes 1
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone


def burn_per_hour(gpu_price: float, max_gpu_nodes: int, cpu_price: float = 0.0,
                  cpu_nodes: int = 0, other_hourly: float = 0.0) -> float:
    return max_gpu_nodes * gpu_price + cpu_nodes * cpu_price + other_hourly


def plan(budget: float, burn: float, reserve: float = 0.15) -> dict:
    if budget <= 0 or burn <= 0:
        raise ValueError("budget and burn must be positive")
    if not 0 <= reserve < 1:
        raise ValueError("reserve must be in [0, 1)")
    hours_total = budget / burn
    return {"budget_usd": budget, "burn_usd_per_hour": burn, "reserve_fraction": reserve,
            "hours_until_budget_gone": hours_total,
            "hours_until_hard_stop": hours_total * (1 - reserve),
            "usd_at_hard_stop": budget * (1 - reserve)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget", type=float, required=True, help="USD for the whole session")
    ap.add_argument("--gpu-price", type=float, required=True, help="USD/hour for one GPU node")
    ap.add_argument("--max-gpu-nodes", type=int, required=True)
    ap.add_argument("--cpu-price", type=float, default=0.0, help="USD/hour for one CPU node")
    ap.add_argument("--cpu-nodes", type=int, default=0)
    ap.add_argument("--other-hourly", type=float, default=0.0)
    ap.add_argument("--reserve", type=float, default=0.15)
    ap.add_argument("--planned-minutes", type=float,
                    help="session length you intend; warns if it does not fit")
    a = ap.parse_args(argv)
    try:
        burn = burn_per_hour(a.gpu_price, a.max_gpu_nodes, a.cpu_price, a.cpu_nodes, a.other_hourly)
        p = plan(a.budget, burn, a.reserve)
    except ValueError as e:
        sys.exit(str(e))
    now = datetime.now(timezone.utc)
    stop = now + timedelta(hours=p["hours_until_hard_stop"])
    print(f"budget                 ${a.budget:.2f}")
    print(f"worst-case burn        ${burn:.2f}/hour "
          f"({a.max_gpu_nodes} GPU x ${a.gpu_price:.4f} + {a.cpu_nodes} CPU x ${a.cpu_price:.4f} "
          f"+ ${a.other_hourly:.4f})")
    print(f"budget gone after      {p['hours_until_budget_gone'] * 60:.0f} min at worst-case burn")
    print(f"HARD STOP after        {p['hours_until_hard_stop'] * 60:.0f} min "
          f"({a.reserve:.0%} held back for teardown)")
    print(f"if you start now       hard stop at {stop:%Y-%m-%d %H:%M} UTC")
    if a.planned_minutes is not None:
        cost = burn * a.planned_minutes / 60
        verdict = "fits" if a.planned_minutes <= p["hours_until_hard_stop"] * 60 else "DOES NOT FIT"
        print(f"planned session        {a.planned_minutes:.0f} min = ${cost:.2f} at worst-case burn: {verdict}")
        if verdict != "fits":
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
