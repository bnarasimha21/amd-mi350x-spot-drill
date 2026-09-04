#!/usr/bin/env python3
"""Project the measured 8B drain onto larger models — "what if I'm fine-tuning 70B?"

Every rate here was measured on the drill (see results/mi350-a/timings.csv); nothing is
borrowed from a spec sheet. The projection is the pessimistic one: single-node upload,
exactly as the drill ran it. Note "single-node" is not "single-stream" — uploader.py runs
~32 concurrent parts (8 per file x 4 files). Sharding across ranks in a multi-node job would
beat these numbers, but we did not measure that, so we don't claim it.

Bigger caveat than any of the rates: at 70B the checkpoint is sharded across GPUs, which is a
different code path from the single-GPU torch.save these numbers came from. The byte count
scales; treat the minutes as an upper bound.

    python3 scale_model.py            # the table
    python3 scale_model.py --crossover  # at what size does the drain outrun the notice?
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from typing import NamedTuple

# --- measured on mi350-a, 2026-08-18 (results/mi350-a/timings.csv) --------------------
CKPT_BYTES = 98_289_353_505          # Qwen3-8B, fp32 master + Adam m,v
MODEL_PARAMS = 8.19e9                # Qwen3-8B
WRITE_S = 56.45                      # median of 4 writes to local NVMe scratch
UPLOAD_S = 257.6                     # median of 4 uploads to Spaces, same region
DOWNLOAD_S = 243.7                   # pull onto the replacement node
RESUME_LOAD_S = 40.0                 # load + cursor/RNG verify

BYTES_PER_PARAM = CKPT_BYTES / MODEL_PARAMS      # ~12.0 = fp32 master(4) + m(4) + v(4)
WRITE_BPS = CKPT_BYTES / WRITE_S                 # ~1.74 GB/s
UPLOAD_BPS = CKPT_BYTES / UPLOAD_S               # ~0.38 GB/s = 3.05 Gbps
DOWNLOAD_BPS = CKPT_BYTES / DOWNLOAD_S

NOTICE_S = 2 * 3600                  # DO's best-effort target

# HBM needed for full fine-tuning with Adam, bf16 compute + fp32 master weights:
#   bf16 params 2 + bf16 grads 2 + fp32 master 4 + Adam m 4 + Adam v 4 = 16 B/param,
# before activations. Sharded (ZeRO-3/FSDP) this divides across ranks.
TRAIN_BYTES_PER_PARAM = 16

MODELS = [
    ("Qwen3-8B (measured)", 8.19e9),
    ("Llama-3.1-13B", 13e9),
    ("Qwen3-32B", 32.8e9),
    ("Llama-3.1-70B", 70.6e9),
    ("Qwen3-235B (MoE total)", 235e9),
]

class Box(NamedTuple):
    """One 8-GPU DigitalOcean AMD box.

    Scope is deliberately AMD-only, matching the video: the comparison that earns screen
    time is "newest spot silicon vs the older on-demand chip", which is MI350X/MI355X
    against MI300X/MI325X. Ranking the whole catalogue by dollars-per-capability drags in
    cross-vendor FLOPS claims that need equal-MFU assumptions we can't defend. (Worth
    knowing but not filming: the spot tier also carries NVIDIA's B300, and the on-demand
    tier carries AMD parts — so spot is not an "AMD thing".)

    tflops is peak DENSE BF16 per GPU, no 2:4 sparsity. AMD publishes dense and sparse as
    separate rows; always take the dense one.

    price_8x is a FALLBACK. Spot pricing changes daily, so live prices are fetched from
    doctl at runtime and these are only used when that fails.
    """

    name: str
    slug: str        # 1x slug; the 8x box is exactly 8 times the 1x price
    hbm_gb: int      # per GPU
    price_8x: float  # $/hr for the 8-GPU box — fallback only, see fetch_prices()
    tier: str
    tflops: float    # dense BF16, per GPU


# Fallback prices observed 2026-08-24. Spot rates move daily — see PRICE_ASOF.
PRICE_ASOF = "2026-08-24"
MI350X = Box("MI350X 8x", "gpu-mi350x1-288gb-spot", 288, 32.00, "spot", 2300.0)
MI355X = Box("MI355X 8x", "gpu-mi355x1-288gb-spot", 288, 36.00, "spot", 2500.0)
MI325X = Box("MI325X 8x", "gpu-mi325x1-256gb", 256, 30.40, "on-demand", 1307.4)
MI300X = Box("MI300X 8x", "gpu-mi300x1-192gb", 192, 20.72, "on-demand", 1307.4)

GPUS = [MI350X, MI355X, MI325X, MI300X]


def fetch_prices() -> tuple[dict[str, float], str]:
    """Ask DigitalOcean what these cost right now.

    Spot pricing "may change daily", so hard-coding it into a script or a doc guarantees a
    stale number on camera. Returns 8x prices keyed by box name, plus a provenance string
    to print so the reader always knows whether they're seeing live or fallback figures.
    """
    try:
        import json
        import subprocess

        env = {}
        for line in open("../.env" if os.path.exists("../.env") else ".env"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        out = subprocess.run(
            ["doctl", "compute", "size", "list", "-o", "json"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DIGITALOCEAN_ACCESS_TOKEN": env.get("DO_TOKEN", "")},
        )
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:80])
        live = {s["slug"]: s["price_hourly"] for s in json.loads(out.stdout) if s.get("price_hourly")}
        prices = {b.name: live[b.slug] * 8 for b in GPUS if b.slug in live}
        if len(prices) != len(GPUS):
            raise RuntimeError("some slugs missing from the live catalogue")
        return prices, f"LIVE from doctl, {datetime.now():%Y-%m-%d %H:%M}"
    except Exception as exc:  # noqa: BLE001 - any failure just means fall back
        return ({b.name: b.price_8x for b in GPUS},
                f"FALLBACK snapshot {PRICE_ASOF} (live lookup failed: {exc})")

# Model FLOPs Utilisation. We never measured this — it is the dominant uncertainty in every
# training-time number below, so results are always shown as a range, never a point estimate.
MFU_RANGE = (0.30, 0.35, 0.40)


def fmt_min(s: float) -> str:
    return f"{s/60:.1f} min" if s < 3600 else f"{s/3600:.2f} h"


def drain(params: float) -> tuple[float, float, float]:
    size = params * BYTES_PER_PARAM
    return size, size / WRITE_BPS, size / UPLOAD_BPS


def train_estimate() -> int:
    """How long, and how much, to full fine-tune 70B — ESTIMATED, not measured.

    Everything above this line came off the drill's own stopwatch. This function does not:
    we never measured training throughput, so this is spec-sheet arithmetic and is labelled
    that way wherever it surfaces.
    """
    params = 70.6e9
    print("70B FULL FINE-TUNE — ESTIMATE, NOT MEASURED")
    print("  We measured checkpoint and transfer rates on the drill; we did NOT measure")
    print("  training throughput. Below is 6*N*D against vendor peak dense BF16.\n")
    print(f"  model     Llama-3.1-70B ({params/1e9:.1f}B params)")
    print("  FLOPs     6 x N x D  (forward+backward; activation recompute pushes this to ~8ND)")
    print(f"  MFU       {MFU_RANGE[0]:.0%}-{MFU_RANGE[-1]:.0%}, the dominant uncertainty here")
    print("  BF16      DENSE peak — AMD lists 2:4-sparse separately at 2x; ignore that row\n")

    prices, provenance = fetch_prices()
    print(f"  prices    {provenance}\n")
    boxes = [MI350X, MI355X, MI325X, MI300X]
    for tokens, label in ((1e9, "1B tokens (typical SFT)"), (10e9, "10B tokens (heavy)")):
        flops = 6 * params * tokens
        print(f"--- {label}: {flops:.2e} FLOPs")
        hdr = (f"    {'box':16s} {'tier':11s} {'peak dense':>11s} {'hours @35%':>11s} "
               f"{'$/hr':>8s} {'cost @35%':>11s} {'cost range':>16s}")
        print(hdr)
        for box in boxes:
            peak = box.tflops * 8 * 1e12
            price = prices[box.name]
            hrs = [flops / (peak * m) / 3600 for m in MFU_RANGE]
            costs = [h * price for h in hrs]
            print(f"    {box.name:16s} {box.tier:11s} {peak/1e15:9.1f}P {hrs[1]:10.1f}h "
                  f"${price:7.2f} {'$'+format(costs[1],',.0f'):>11s} "
                  f"{'$'+format(costs[2],',.0f')+'-'+format(costs[0],',.0f'):>16s}")
        print()

    # The headline "spot is cheaper" rests entirely on both parts hitting the same MFU. That is
    # a thin assumption in the wrong direction — CDNA 4 is newer, so its ROCm kernels are less
    # tuned than MI300X's — so print how much MFU deficit it takes to erase the gap, and let the
    # margin speak for itself rather than quoting a cost win we can't defend.
    new, old = MI350X, MI300X
    new_ppf = prices[new.name] / (new.tflops * 8 / 1000)
    old_ppf = prices[old.name] / (old.tflops * 8 / 1000)
    ratio = new_ppf / old_ppf
    print("HOW FRAGILE IS THE COST ADVANTAGE?")
    print(f"  {new.name:12s} ${new_ppf:5.2f} per PFLOP-hr")
    print(f"  {old.name:12s} ${old_ppf:5.2f} per PFLOP-hr")
    print(f"  -> {new.name} is {(1-ratio)*100:.1f}% cheaper per unit of compute AT EQUAL MFU,")
    print(f"     so it breaks even at {ratio:.0%} of {old.name}'s MFU "
          f"({0.35*ratio:.1%} if the older part hits 35%).")
    print("     CDNA 4 has less-mature ROCm coverage than MI300X, so that deficit is plausible.")
    print("     LEAD WITH TIME, NOT MONEY: 'same ballpark cost, roughly half the wall clock'")
    print("     survives any sane MFU spread; a 12% cost win does not.\n")
    print("Add the reclamation tax: a 70B run that gets reclaimed loses ~1.4 h to recovery,")
    print("and bills two boxes briefly during the overlap.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crossover", action="store_true")
    ap.add_argument("--train", action="store_true", help="70B training time + cost (ESTIMATE)")
    args = ap.parse_args()

    if args.train:
        return train_estimate()

    print("measured on MI350X, single stream, same-region Spaces")
    print(f"  checkpoint    {BYTES_PER_PARAM:.1f} B/param   (fp32 master + Adam m,v)")
    print(f"  write         {WRITE_BPS/1e9:.2f} GB/s to local NVMe scratch")
    print(f"  upload        {UPLOAD_BPS*8/1e9:.2f} Gbps to Spaces")
    print(f"  download      {DOWNLOAD_BPS*8/1e9:.2f} Gbps from Spaces")
    print(f"  notice target {NOTICE_S/3600:.0f} h (best-effort, not a guarantee)\n")

    hdr = f"{'model':24s} {'ckpt':>9s} {'write':>9s} {'upload':>10s} {'DRAIN':>10s} {'% of 2h':>8s}"
    print(hdr); print("-" * len(hdr))
    for name, p in MODELS:
        size, w, u = drain(p)
        tot = w + u
        print(f"{name:24s} {size/1e9:8.0f}G {fmt_min(w):>9s} {fmt_min(u):>10s} "
              f"{fmt_min(tot):>10s} {tot/NOTICE_S*100:7.0f}%")

    print("\nfull recovery (drain + provision + pull + resume), replacement assumed available:")
    print("  provision create->GPU 201 s measured; bootstrap 96 s measured")
    for name, p in [m for m in MODELS if m[1] in (8.19e9, 70.6e9)]:
        size, w, u = drain(p)
        pull = size / DOWNLOAD_BPS
        total = w + u + 201 + 96 + pull + RESUME_LOAD_S
        print(f"  {name:24s} {fmt_min(total)}  (drain {fmt_min(w+u)} + pull {fmt_min(pull)})")

    print("\nHBM needed to FULL fine-tune (16 B/param + activations), vs one 8-GPU box:")
    hdr2 = f"{'model':24s} {'needs':>8s}  " + "  ".join(f"{b.name:>10s}" for b in GPUS)
    print(hdr2); print("-" * len(hdr2))
    for name, p in MODELS:
        need = p * TRAIN_BYTES_PER_PARAM / 1e9
        cells = [f"{'fits' if need < b.hbm_gb * 8 * 0.8 else 'NO':>10s}" for b in GPUS]
        print(f"{name:24s} {need:7.0f}G  " + "  ".join(cells))
    print("  ('fits' = model states under 80% of box HBM, leaving room for activations)")

    prices, provenance = fetch_prices()
    print(f"\n$/hr for an 8-GPU box — {provenance}")
    print("  (no volume discount: 8x is exactly 8 times the 1x rate)")
    for b in GPUS:
        total_hbm = b.hbm_gb * 8
        price = prices[b.name]
        print(f"  {b.name:12s} {total_hbm:5d} GB HBM  ${price:6.2f}/hr  "
              f"${price/total_hbm*1000:5.2f}/TB-hr  {b.tier}")

    if args.crossover:
        # Where does write+upload consume the entire notice window?
        per_param = BYTES_PER_PARAM * (1 / WRITE_BPS + 1 / UPLOAD_BPS)
        n = NOTICE_S / per_param
        print(f"\ncrossover: at ~{n/1e9:.0f}B params the single-stream drain consumes the")
        print(f"           whole {NOTICE_S/3600:.0f}-hour notice. Past that, periodic checkpointing")
        print("           stops being optional and sharded parallel upload is mandatory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
