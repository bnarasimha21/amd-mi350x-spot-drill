#!/usr/bin/env python3
"""Resumable full fine-tune of Qwen3-8B — the video's 5-part checkpoint, for real.

Checkpoint = (1) model weights (2) optimizer state incl. Adam m,v (3) LR-scheduler step
             (4) data cursor (epoch + global step, seeded shuffle) (5) RNG states.

Precision policy (matches the video's byte accounting): fp32 master weights + autocast
bf16 forward -> optimizer m,v are true fp32. Checkpoint ~= 12 B/param (~96 GB for 8B);
the script's 14-16 B/param figure additionally counts a bf16 serving copy.

Signals:  SIGUSR1 = drain (finish step, checkpoint tag='drain', exit 0)
          SIGUSR2 = manual checkpoint-and-continue (phase 2 timing samples)
Resume:   --resume auto -> load latest ckpt -> assert cursor + RNG match prediction
          -> print 'RESUME OK step=N'. Every loss is appended to losses.jsonl so a
          same-box resume rehearsal (phase 2b) can prove loss-for-loss continuity.
"""
import argparse, hashlib, json, os, random, signal, sys, time, urllib.request
import numpy as np
import torch
from transformers import (AutoModelForCausalLM, AutoTokenizer,
                          get_cosine_schedule_with_warmup)
from datasets import load_dataset

CKPT_ROOT = "/mnt/scratch/ckpt"
TIMINGS = os.path.join(CKPT_ROOT, "timings.csv")
LOSSES = os.path.join(CKPT_ROOT, "losses.jsonl")
FLAGS = {"drain": False, "manual_ckpt": False}
signal.signal(signal.SIGUSR1, lambda *_: FLAGS.update(drain=True))
signal.signal(signal.SIGUSR2, lambda *_: FLAGS.update(manual_ckpt=True))


def post_event(d):
    try:
        body = json.dumps(d).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"http://{os.environ['CONTROLLER_IP']}:{os.environ['CONTROLLER_PORT']}/event",
            body, {"Content-Type": "application/json",
                   "X-Drill-Token": os.environ.get("DRILL_TOKEN", "")}), timeout=5)
    except Exception:
        pass  # controller reporting is best-effort; local files are the fallback record


def log_timing(event, dur_s=None, size_bytes=None):
    with open(TIMINGS, "a") as f:
        f.write(f"{int(time.time()*1000)},{event},{dur_s or ''},{size_bytes or ''}\n")
    print(f"[timing] {event} dur={dur_s} bytes={size_bytes}", flush=True)
    post_event({"event": event, "node": os.uname().nodename,
                "dur_s": dur_s, "bytes": size_bytes})


def rng_fingerprint():
    h = hashlib.sha256()
    h.update(torch.get_rng_state().numpy().tobytes())
    h.update(torch.cuda.get_rng_state().cpu().numpy().tobytes())
    h.update(np.random.get_state()[1].tobytes())
    h.update(str(random.getstate()[1]).encode())
    return h.hexdigest()[:16]


def epoch_permutation(seed, epoch, n):
    return np.random.default_rng(seed + epoch).permutation(n)


def batch_ids(perm, step_in_epoch, bs):
    return perm[step_in_epoch * bs:(step_in_epoch + 1) * bs].tolist()


def fsync_write(path, write_fn):
    with open(path, "wb" if path.endswith(".pt") else "w") as f:
        write_fn(f)
        f.flush()
        os.fsync(f.fileno())


def save_checkpoint(tag, model, opt, sched, args, epoch, gstep, n_samples):
    os.makedirs(CKPT_ROOT, exist_ok=True)
    path = os.path.join(CKPT_ROOT, f"step{gstep:07d}")
    tmp = os.path.join(CKPT_ROOT, f".tmp-step{gstep:07d}")  # dot-prefix: invisible to step* globs
    os.makedirs(tmp, exist_ok=True)
    t0 = time.time()
    torch.save(model.state_dict(), os.path.join(tmp, "model.pt"))          # (1) fp32 master
    torch.save(opt.state_dict(), os.path.join(tmp, "optimizer.pt"))        # (2) fp32 m,v
    torch.save(sched.state_dict(), os.path.join(tmp, "scheduler.pt"))      # (3)
    torch.save({"torch": torch.get_rng_state(), "cuda": torch.cuda.get_rng_state(),
                "numpy": np.random.get_state(), "python": random.getstate()},
               os.path.join(tmp, "rng.pt"))                                # (5)
    steps_per_epoch = n_samples // args.batch_size
    next_step_in_epoch = gstep % steps_per_epoch
    # Epoch-boundary fix: if the next step starts a new epoch, the cursor must say so
    resume_epoch = epoch + 1 if next_step_in_epoch == 0 else epoch
    perm = epoch_permutation(args.seed, resume_epoch, n_samples)
    meta = {"tag": tag, "epoch": resume_epoch, "global_step": gstep,      # (4)
            "expected_next_ids": batch_ids(perm, next_step_in_epoch, args.batch_size),
            "rng_fingerprint": rng_fingerprint()}
    fsync_write(os.path.join(tmp, "meta.json"), lambda f: json.dump(meta, f, indent=2))
    for fn in ("model.pt", "optimizer.pt", "scheduler.pt", "rng.pt"):
        fd = os.open(os.path.join(tmp, fn), os.O_RDONLY)
        os.fsync(fd); os.close(fd)
    os.replace(tmp, path)  # atomic visibility...
    dirfd = os.open(CKPT_ROOT, os.O_RDONLY)
    os.fsync(dirfd); os.close(dirfd)  # ...and durable rename
    dur = time.time() - t0
    size = sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))
    log_timing(f"ckpt_write_{tag}", round(dur, 1), size)
    print(f"CHECKPOINT COMPLETE {path} ({size/2**30:.1f} GiB in {dur:.1f}s)", flush=True)
    return path


def load_checkpoint(model, opt, sched, args, n_samples):
    ckpts = sorted(d for d in os.listdir(CKPT_ROOT)
                   if d.startswith("step") and
                   os.path.exists(os.path.join(CKPT_ROOT, d, "meta.json")))
    assert ckpts, "no complete checkpoint found for --resume"
    path = os.path.join(CKPT_ROOT, ckpts[-1])
    model.load_state_dict(torch.load(os.path.join(path, "model.pt"), weights_only=True))
    opt.load_state_dict(torch.load(os.path.join(path, "optimizer.pt"), weights_only=True))
    sched.load_state_dict(torch.load(os.path.join(path, "scheduler.pt"), weights_only=True))
    # weights_only=False required: numpy/python RNG states aren't tensors (torch>=2.6 semantics)
    rng = torch.load(os.path.join(path, "rng.pt"), weights_only=False)
    torch.set_rng_state(rng["torch"]); torch.cuda.set_rng_state(rng["cuda"])
    np.random.set_state(rng["numpy"]); random.setstate(rng["python"])
    meta = json.load(open(os.path.join(path, "meta.json")))
    steps_per_epoch = n_samples // args.batch_size
    perm = epoch_permutation(args.seed, meta["epoch"], n_samples)
    got_ids = batch_ids(perm, meta["global_step"] % steps_per_epoch, args.batch_size)
    assert got_ids == meta["expected_next_ids"], "data cursor mismatch on resume!"
    assert rng_fingerprint() == meta["rng_fingerprint"], "RNG state mismatch on resume!"
    print(f"RESUME OK step={meta['global_step']} (cursor + RNG verified)", flush=True)
    post_event({"event": "resume_ok", "node": os.uname().nodename,
                "step": meta["global_step"]})
    return meta["epoch"], meta["global_step"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="Qwen/Qwen3-8B")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--seq-len", type=int, default=1024)
    p.add_argument("--lr", type=float, default=1e-5)
    p.add_argument("--max-steps", type=int, default=100000)
    p.add_argument("--ckpt-every", type=int, default=0, help="0 = only on signal")
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--resume", choices=["auto", "no"], default="no")
    args = p.parse_args()

    torch.manual_seed(args.seed); np.random.seed(args.seed); random.seed(args.seed)
    os.makedirs(CKPT_ROOT, exist_ok=True)
    with open("/run/trainer.pid", "w") as f:
        f.write(str(os.getpid()))

    tok = AutoTokenizer.from_pretrained(args.model)
    assert tok.pad_token is not None or tok.eos_token is not None, "no pad/eos token"
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    ds = load_dataset("yahma/alpaca-cleaned", split="train")
    texts = [f"{r['instruction']}\n{r['input']}\n{r['output']}" for r in ds]
    n = (len(texts) // args.batch_size) * args.batch_size

    # fp32 master weights; bf16 compute via autocast -> AdamW state is true fp32
    model = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype=torch.float32, device_map="cuda")
    model.gradient_checkpointing_enable()
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    sched = get_cosine_schedule_with_warmup(opt, 100, args.max_steps)

    epoch, gstep = 0, 0
    if args.resume == "auto":
        epoch, gstep = load_checkpoint(model, opt, sched, args, n)

    steps_per_epoch = n // args.batch_size
    model.train()
    try:
        while gstep < args.max_steps:
            perm = epoch_permutation(args.seed, epoch, n)
            for s in range(gstep % steps_per_epoch, steps_per_epoch):
                ids = batch_ids(perm, s, args.batch_size)
                batch = tok([texts[i] for i in ids], return_tensors="pt", padding=True,
                            truncation=True, max_length=args.seq_len).to("cuda")
                labels = batch["input_ids"].masked_fill(batch["attention_mask"] == 0, -100)
                with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                    out = model(**batch, labels=labels)
                out.loss.backward()
                opt.step(); sched.step(); opt.zero_grad(set_to_none=True)
                gstep += 1
                loss = out.loss.item()
                print(f"step={gstep} epoch={epoch} loss={loss:.4f}", flush=True)
                with open(LOSSES, "a") as f:
                    f.write(json.dumps({"step": gstep, "loss": round(loss, 6),
                                        "host": os.uname().nodename}) + "\n")

                if FLAGS["drain"]:
                    print("DRAIN received — checkpointing and exiting clean", flush=True)
                    save_checkpoint("drain", model, opt, sched, args, epoch, gstep, n)
                    return
                if FLAGS["manual_ckpt"]:
                    FLAGS["manual_ckpt"] = False
                    save_checkpoint("manual", model, opt, sched, args, epoch, gstep, n)
                if args.ckpt_every and gstep % args.ckpt_every == 0:
                    save_checkpoint("periodic", model, opt, sched, args, epoch, gstep, n)
                if gstep >= args.max_steps:
                    return
            epoch += 1
    finally:
        try:
            os.remove("/run/trainer.pid")  # never leave a stale PID for the agent
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
