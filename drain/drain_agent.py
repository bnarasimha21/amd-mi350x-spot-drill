#!/usr/bin/env python3
"""On-box drain agent: polls the controller; on 'draining' (scoped to THIS node or global)
executes the drain choreography:

  notice -> SIGUSR1 to trainer -> wait for trainer EXIT -> pick the ckpt tagged 'drain'
         -> timed upload to Spaces -> report 'node_safe' -> idle awaiting destroy.

Resilient by design: network errors retry forever (a dead agent = a hung drill), the PID
is verified to actually be train.py before signaling, and .tmp/partial checkpoints are
never eligible (trainer stages under a dot-prefixed dir; we also require meta.json).
Run via run_node.sh, which sources /opt/drill/.env first.
"""
import glob, json, os, signal, subprocess, sys, time, urllib.request

E = os.environ
CTRL = f"http://{E['CONTROLLER_IP']}:{E['CONTROLLER_PORT']}"
CKPT_ROOT = "/mnt/scratch/ckpt"
NODE = os.uname().nodename
HEADERS = {"Content-Type": "application/json", "X-Drill-Token": E.get("DRILL_TOKEN", "")}


def get(path):
    while True:  # transient controller/network errors must never kill the agent
        try:
            req = urllib.request.Request(f"{CTRL}{path}", headers=HEADERS)
            return json.load(urllib.request.urlopen(req, timeout=5))
        except Exception as e:
            print(f"[agent] controller unreachable ({e}); retrying in 5s", flush=True)
            time.sleep(5)


def post_event(d):
    d["node"] = NODE
    for _ in range(5):
        try:
            body = json.dumps(d).encode()
            urllib.request.urlopen(urllib.request.Request(
                f"{CTRL}/event", body, HEADERS), timeout=5)
            return
        except Exception:
            time.sleep(3)
    print(f"[agent] WARNING: event not delivered: {d}", flush=True)


def complete_ckpts():
    return sorted(d for d in glob.glob(os.path.join(CKPT_ROOT, "step[0-9]*"))
                  if os.path.exists(os.path.join(d, "meta.json")))


def trainer_pid():
    try:
        pid = int(open("/run/trainer.pid").read().strip())
        cmdline = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore")
        return pid if "train.py" in cmdline else None
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None


def main():
    print(f"[agent] {NODE}: polling {CTRL} every 5s", flush=True)
    while True:
        s = get(f"/status?node={NODE}")
        if s["status"] == "draining":
            break
        time.sleep(5)

    t0 = time.time()
    post_event({"event": "drain_started"})
    pid = trainer_pid()
    if pid is None:
        post_event({"event": "drain_failed", "reason": "no live train.py process"})
        sys.exit(1)
    os.kill(pid, signal.SIGUSR1)
    print("[agent] SIGUSR1 sent; waiting for trainer to checkpoint and exit...", flush=True)

    while trainer_pid() is not None:  # trainer exits 0 only after os.replace + fsync
        time.sleep(2)
    drains = [d for d in complete_ckpts()
              if json.load(open(os.path.join(d, "meta.json")))["tag"] == "drain"]
    if not drains:
        post_event({"event": "drain_failed", "reason": "trainer exited without drain ckpt"})
        sys.exit(1)
    ckpt = drains[-1]
    post_event({"event": "ckpt_written", "ckpt": os.path.basename(ckpt),
                "since_notice_s": round(time.time() - t0, 1)})

    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__),
                    "..", "trainer", "uploader.py"), "up", ckpt], check=True, env=E)
    post_event({"event": "node_safe", "ckpt": os.path.basename(ckpt),
                "since_notice_s": round(time.time() - t0, 1)})
    print(f"[agent] NODE SAFE — state durable off-box {round(time.time() - t0)}s "
          f"after detection. Awaiting destroy.", flush=True)


if __name__ == "__main__":
    main()
