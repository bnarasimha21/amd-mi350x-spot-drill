#!/usr/bin/env python3
"""Hourly price lookup shared by the serving tools.

There is no price table in this file. A price is either passed in by the operator or read
live from `doctl compute size list`, the same source provision/01_create_spot.sh prints
before it creates a Droplet. Every price comes back with a provenance string, and that
string is written into whatever file the price ends up in.

DOKS bills GPU worker nodes at the GPU Droplet rate for the same size slug, per second, and
locks a Spot node pool's rate when the pool is created
(https://docs.digitalocean.com/products/kubernetes/details/pricing/). A live lookup made
after the pool exists can therefore differ from what the pool is billed. Record the price
at pool creation (serving/cluster/01_create_cluster.sh writes it to .state/) and pass that.

    python3 serving/pricing.py gpu-mi350x1-288gb-spot
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


class PriceError(RuntimeError):
    pass


def _token_from_env_file() -> str:
    """DO_TOKEN from the repo's .env, if there is one. Empty string means use doctl's own auth."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "..", ".env"), ".env"):
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line.startswith("DO_TOKEN=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def parse_size_list(doctl_json: str, slug: str) -> float:
    """Pull one slug's price_hourly out of `doctl compute size list -o json` output."""
    try:
        sizes = json.loads(doctl_json)
    except ValueError as e:
        raise PriceError(f"doctl output is not JSON: {e}") from e
    for s in sizes:
        if s.get("slug") == slug:
            price = s.get("price_hourly")
            if not isinstance(price, (int, float)) or price <= 0:
                raise PriceError(f"{slug} has no usable price_hourly ({price!r})")
            return float(price)
    raise PriceError(f"{slug} is not in the live size catalogue")


def fetch_hourly_price(slug: str) -> tuple[float, str]:
    """Ask doctl what one node of this size costs per hour right now. Raises PriceError."""
    env = dict(os.environ)
    tok = env.get("DIGITALOCEAN_ACCESS_TOKEN") or _token_from_env_file()
    if tok:
        env["DIGITALOCEAN_ACCESS_TOKEN"] = tok
    try:
        out = subprocess.run(["doctl", "compute", "size", "list", "-o", "json"],
                             capture_output=True, text=True, timeout=30, env=env)
    except FileNotFoundError as e:
        raise PriceError("doctl is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise PriceError("doctl timed out") from e
    if out.returncode != 0:
        raise PriceError("doctl failed: " + out.stderr.strip()[:120])
    price = parse_size_list(out.stdout, slug)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return price, f"live from doctl compute size list, {slug}, {stamp}"


def resolve_price(explicit: float | None, slug: str | None, source_note: str | None = None
                  ) -> tuple[float, str]:
    """An explicit price wins. Otherwise look the slug up live. Never falls back to a guess."""
    if explicit is not None:
        if explicit <= 0:
            raise PriceError("price must be positive")
        return float(explicit), "passed by the operator" + (f": {source_note}" if source_note else "")
    if not slug:
        raise PriceError("give --price-hourly, or --size-slug for a live doctl lookup")
    return fetch_hourly_price(slug)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: pricing.py <size-slug>")
    try:
        p, src = fetch_hourly_price(sys.argv[1])
    except PriceError as e:
        sys.exit(f"no price: {e}")
    print(f"{p}\t{src}")
