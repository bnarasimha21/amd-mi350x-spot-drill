#!/usr/bin/env python3
"""Fill the Redis queue with batch-inference jobs, and reap expired leases.

Usage: enqueue.py --fill 60        # 60 prompt jobs
       enqueue.py --reap           # run in a loop during the demo
       enqueue.py --stats
"""
import argparse, json, time, uuid
from qlib import client, Queue

r = client()
q = Queue(r)

PROMPTS = [
    "Summarize the tradeoffs of spot versus on-demand GPU capacity.",
    "Explain checkpoint/restart to a new ML engineer in three sentences.",
    "Write a haiku about a GPU that knows it will be reclaimed.",
    "List five failure modes of distributed training jobs.",
]


def fill(n):
    for i in range(n):
        job = {"id": str(uuid.uuid4()), "seq": i, "prompt": PROMPTS[i % len(PROMPTS)]}
        r.lpush("jobs", json.dumps(job))
    print(f"enqueued {n} jobs (queue depth: {r.llen('jobs')})")


def stats():
    print(f"waiting={r.llen('jobs')} leased={r.zcard('leases')} done={r.hlen('results')}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--fill", type=int)
    p.add_argument("--reap", action="store_true")
    a = p.parse_args()
    if a.fill is not None:
        fill(a.fill)
    elif a.reap:
        while True:
            n = q.reap(time.time())
            if n:
                print(f"REAPED {n} expired lease(s) -> requeued — "
                      f"the 'no ack, no loss' moment", flush=True)
            time.sleep(10)
    else:
        stats()
