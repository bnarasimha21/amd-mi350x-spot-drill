#!/usr/bin/env python3
"""Ephemeral batch-inference worker: lease job -> generate -> write result -> ack.

Results are keyed by job id (idempotent: a requeued job's re-run overwrites identically).
Kill -9 this process mid-job during the demo; the reaper requeues its lease and another
worker finishes it. Uses the model's chat template (thinking disabled) so on-camera
outputs are clean answers, not <think> rambling.

Usage: worker.py --name worker-a [--drain-aware]
"""
import argparse, json, os, time, urllib.request
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from qlib import client, Queue

E = os.environ


def draining(node):
    try:
        s = json.load(urllib.request.urlopen(
            f"http://{E['CONTROLLER_IP']}:{E['CONTROLLER_PORT']}/status?node={node}",
            timeout=3))
        return s["status"] == "draining"
    except Exception:
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--model", default="Qwen/Qwen3-8B")
    p.add_argument("--drain-aware", action="store_true")
    args = p.parse_args()

    r = client()
    q = Queue(r)
    tok = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype=torch.bfloat16, device_map="cuda")
    model.eval()
    node = os.uname().nodename
    print(f"[{args.name}] ready", flush=True)

    while True:
        if args.drain_aware and draining(node):
            print(f"[{args.name}] drain: exiting. Un-acked work will reappear "
                  f"for another worker.", flush=True)
            return
        raw = q.lease(time.time())
        if not raw:
            time.sleep(2)
            continue
        job = json.loads(raw)
        print(f"[{args.name}] working job {job['seq']} ({job['id'][:8]})", flush=True)
        chat = tok.apply_chat_template(
            [{"role": "user", "content": job["prompt"]}],
            tokenize=False, add_generation_prompt=True, enable_thinking=False)
        inputs = tok(chat, return_tensors="pt").to("cuda")
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=200, do_sample=False)
        text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        r.hset("results", job["id"], json.dumps(
            {"seq": job["seq"], "worker": args.name, "text": text}))  # result off-box FIRST
        q.ack(raw)                                                     # ...then ack (original raw)
        print(f"[{args.name}] done job {job['seq']}", flush=True)


if __name__ == "__main__":
    main()
