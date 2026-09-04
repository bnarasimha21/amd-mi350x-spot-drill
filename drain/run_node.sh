#!/usr/bin/env bash
# The ONE way to drive processes on a GPU box.
#
# PyTorch lives inside the drill container (the DO AMD image ships it as a docker image, not
# in system python), so trainer AND agent both run there — the drain choreography signals the
# trainer by PID, which requires a shared PID namespace.
#
# Usage (on the box):  run_node.sh train [--resume auto] [--max-steps N] [--ckpt-every N]
#                      run_node.sh agent
#                      run_node.sh ckpt              # SIGUSR2: checkpoint-and-continue
#                      run_node.sh push <ckpt-dir>   # upload to Spaces (timed)
#                      run_node.sh pull <ckpt-name>  # download from Spaces (timed)
#                      run_node.sh stop | status | exec <cmd...>
set -euo pipefail
cd /opt/drill
set -a; . ./.env; set +a
C="${DRILL_CONTAINER:-drill}"
LOGS=/mnt/scratch/logs

docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null | grep -q true || {
  echo "FATAL: container '$C' is not running. Re-run 02_bootstrap.sh."; exit 1; }

dexec() { docker exec "$C" "$@"; }

# Bracket trick throughout: `pkill -f 'trainer/train.py'` also matches the `bash -c` that is
# running the pkill (its own cmdline contains the pattern), so it kills itself mid-command --
# the trainer may or may not die first, and `set -e` aborts before the confirmation prints.
# '[t]rainer' matches the literal text "trainer" but NOT the string "[t]rainer" in our cmdline.
TRAIN_PAT='[t]rainer/train\.py'
AGENT_PAT='[d]rain/drain_agent\.py'
alive() { dexec bash -c "pgrep -f '$1' >/dev/null" 2>/dev/null; }

case "${1:?usage: run_node.sh train|agent|ckpt|push|pull|stop|status|exec}" in
  train)
    shift
    # A second trainer on the same GPU silently interleaves into losses.jsonl and doubles VRAM.
    alive "$TRAIN_PAT" && { echo "REFUSING: a trainer is already running (run_node.sh stop first)"; exit 1; }
    docker exec -d "$C" bash -c "cd /opt/drill && python3 trainer/train.py $* >> $LOGS/train.log 2>&1"
    echo "trainer started in container $C — log: /var/log/train.log"
    ;;
  agent)
    alive "$AGENT_PAT" && { echo "REFUSING: an agent is already running (run_node.sh stop first)"; exit 1; }
    docker exec -d "$C" bash -c "cd /opt/drill && python3 drain/drain_agent.py >> $LOGS/agent.log 2>&1"
    echo "agent started in container $C — log: /var/log/agent.log"
    ;;
  ckpt)
    dexec bash -c 'kill -USR2 "$(cat /run/trainer.pid)"'
    echo "SIGUSR2 sent — checkpoint-and-continue"
    ;;
  push)
    dexec python3 /opt/drill/trainer/uploader.py up "${2:?usage: run_node.sh push <ckpt-dir>}"
    ;;
  pull)
    dexec mkdir -p /mnt/scratch/ckpt
    dexec python3 /opt/drill/trainer/uploader.py down \
      "${2:?usage: run_node.sh pull <ckpt-name>}" /mnt/scratch/ckpt
    ;;
  stop)
    dexec bash -c "pkill -f '$TRAIN_PAT' || true; pkill -f '$AGENT_PAT' || true"
    # Block until they're actually gone. Returning early races the caller: the dying trainer
    # keeps appending to losses.jsonl, which corrupts any "lines after N" comparison.
    for _ in $(seq 30); do
      alive "$TRAIN_PAT" || alive "$AGENT_PAT" || { echo "trainer + agent stopped"; exit 0; }
      sleep 1
    done
    echo "still alive after 30s — escalating to SIGKILL"
    dexec bash -c "pkill -9 -f '$TRAIN_PAT' || true; pkill -9 -f '$AGENT_PAT' || true"
    sleep 2
    # `a || b && c` under set -e exits the script when nothing is alive, so use an if.
    if alive "$TRAIN_PAT" || alive "$AGENT_PAT"; then
      echo "FATAL: processes survived SIGKILL"; exit 1
    fi
    echo "trainer + agent stopped (SIGKILL)"
    ;;
  status)
    docker ps --filter "name=$C" --format 'container: {{.Names}} {{.Status}}'
    dexec bash -c 'ps -eo pid,etime,cmd | grep -E "train\.py|drain_agent\.py" | grep -v grep || echo "(no drill processes running)"'
    ;;
  exec)
    shift; dexec "$@"
    ;;
  *)
    echo "unknown subcommand '$1'"; exit 1
    ;;
esac
