#!/usr/bin/env python3
"""Timed parallel transfer between local checkpoint dir and DO Spaces.

Parallelism on two axes (closer to a real sharded-checkpoint uploader): files upload
concurrently, each file multipart-concurrent. Single-flight via flock so a drain upload
can never contend with a periodic/manual upload — the newcomer waits for the lock, and
the drain agent's upload of the FINAL checkpoint is the one that lands last.

Usage:  uploader.py up   <local-ckpt-dir>
        uploader.py down <ckpt-name> <local-root>
Prints duration + effective Gbps; appends to timings.csv and reports to the controller.
"""
import fcntl, json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
import boto3
from boto3.s3.transfer import TransferConfig

E = os.environ
cfg = TransferConfig(multipart_threshold=64 * 2**20, multipart_chunksize=64 * 2**20,
                     max_concurrency=8, use_threads=True)  # 8/file x 4 files ≈ 32 streams
s3 = boto3.client("s3", endpoint_url=E["SPACES_ENDPOINT"],
                  aws_access_key_id=E["SPACES_KEY"], aws_secret_access_key=E["SPACES_SECRET"])
BUCKET = E["SPACES_BUCKET"]
LOCK = "/mnt/scratch/ckpt/.upload.lock"


def report(event, dur, size):
    gbps = size * 8 / dur / 1e9 if dur else 0
    print(f"{event}: {size/2**30:.1f} GiB in {dur:.1f}s = {gbps:.2f} Gbps", flush=True)
    with open("/mnt/scratch/ckpt/timings.csv", "a") as f:
        f.write(f"{int(time.time()*1000)},{event},{dur:.1f},{size}\n")
    try:
        body = json.dumps({"event": event, "node": os.uname().nodename,
                           "dur_s": round(dur, 1), "bytes": size,
                           "gbps": round(gbps, 2)}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"http://{E['CONTROLLER_IP']}:{E['CONTROLLER_PORT']}/event", body,
            {"Content-Type": "application/json",
             "X-Drill-Token": E.get("DRILL_TOKEN", "")}), timeout=5)
    except Exception:
        pass  # best-effort; timings.csv is the local record


def up(local_dir):
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # single-flight (item: drain vs periodic contention)
        name = os.path.basename(local_dir.rstrip("/"))
        files = [(os.path.join(local_dir, f), f) for f in os.listdir(local_dir)]
        size = sum(os.path.getsize(p) for p, _ in files)
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(s3.upload_file, path, BUCKET, f"{name}/{key}", Config=cfg)
                    for path, key in files]
            for f in futs:
                f.result()  # raise on any failure BEFORE stamping _COMPLETE
        s3.put_object(Bucket=BUCKET, Key=f"{name}/_COMPLETE", Body=b"ok")
        report("ckpt_upload", time.time() - t0, size)


def down(name, local_root):
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=BUCKET, Prefix=f"{name}/") for o in page.get("Contents", [])]
    assert f"{name}/_COMPLETE" in keys, f"checkpoint {name} incomplete in Spaces"
    dest = os.path.join(local_root, name)
    os.makedirs(dest, exist_ok=True)
    t0 = time.time()
    keys = [k for k in keys if not k.endswith("/_COMPLETE")]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(s3.download_file, BUCKET, k,
                          os.path.join(dest, os.path.basename(k)), Config=cfg): k
                for k in keys}
        for f in futs:
            f.result()
    size = sum(os.path.getsize(os.path.join(dest, os.path.basename(k))) for k in keys)
    report("ckpt_download", time.time() - t0, size)


if __name__ == "__main__":
    {"up": lambda: up(sys.argv[2]),
     "down": lambda: down(sys.argv[2], sys.argv[3])}[sys.argv[1]]()
