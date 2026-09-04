#!/usr/bin/env python3
"""Scene 5's numbers, straight from timings.csv — the n=3 checkpoint write + upload table.

train.py logs the write; uploader.py logs the upload, and only to timings.csv (its stdout goes
to whoever ran `run_node.sh push`). So this is the only place both halves appear together.

Usage: timings.py
"""
import csv, os, statistics as st

TIMINGS = "/mnt/scratch/ckpt/timings.csv"
rows = []
with open(TIMINGS) as f:
    for r in csv.reader(f):
        if len(r) >= 4 and r[2] and r[3]:
            rows.append((r[1], float(r[2]), int(r[3])))

writes = [r for r in rows if r[0].startswith("ckpt_write")]
ups = [r for r in rows if r[0] == "ckpt_upload"]
sized = writes or ups
size = sized[0][2] if sized else 0

print(f"checkpoint size : {size:,} bytes = {size/2**30:.1f} GiB = {size/1e9:.1f} GB")
print(f"                  {size/8.19e9:.1f} B/param on Qwen3-8B (8.19 B params)\n")
print(f"{'#':>2}  {'write (s)':>10}  {'upload (s)':>11}  {'Gbps':>6}")
for i in range(max(len(writes), len(ups))):
    w = f"{writes[i][1]:.1f}" if i < len(writes) else "-"
    if i < len(ups):
        u, g = f"{ups[i][1]:.1f}", f"{ups[i][2]*8/ups[i][1]/1e9:.2f}"
    else:
        u = g = "-"
    print(f"{i+1:>2}  {w:>10}  {u:>11}  {g:>6}")

if writes and ups:
    mw, mu = st.median(w[1] for w in writes), st.median(u[1] for u in ups)
    mg = st.median(u[2] * 8 / u[1] / 1e9 for u in ups)
    print(f"{'med':>2}  {mw:>10.1f}  {mu:>11.1f}  {mg:>6.2f}")
    print(f"\ncheckpoint -> durable in object storage: {(mw+mu)/60:.1f} min, "
          f"against a 2-hour reclamation notice")
