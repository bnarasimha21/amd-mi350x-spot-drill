#!/usr/bin/env bash
# Bootstrap a spot GPU droplet.
#
# PHASE-0/1 FINDING: the DO AMD AI/ML images do NOT put PyTorch in the system python. They
# ship it as a docker image (rocm/pytorch:...) and run a Jupyter container off it. So the
# drill gets its OWN long-lived container off that same image, with scratch + code bind-
# mounted. Trainer and agent both live in that container: the drain choreography sends
# SIGUSR1 to a PID, which only works inside one shared PID namespace.
#
# Usage: 02_bootstrap.sh <droplet-ip>
set -euo pipefail
cd "$(dirname "$0")/.." && source .env
IP="${1:?usage: 02_bootstrap.sh <droplet-ip>}"
: "${SCRATCH_DEV:?set SCRATCH_DEV in .env (phase-0 recon found /dev/vdc1)}"
: "${DRILL_IMAGE:?set DRILL_IMAGE in .env}"
: "${DRILL_CONTAINER:?set DRILL_CONTAINER in .env}"

echo "Shipping code to $IP..."
ssh root@"$IP" "mkdir -p /opt/drill"
# PHASE-3 FINDING: 01_create_spot.sh's "real shell" gate is NOT sufficient for scp. The
# marketplace first-boot wrapper lets `ssh <cmd>` through while still closing the SFTP
# subsystem, so the very next scp dies with "scp: Connection closed" and takes the whole
# drill down with it. Retry until file transfer actually works.
for i in $(seq 1 60); do
  scp -q -r trainer drain queue_demo requirements.txt root@"$IP":/opt/drill/ 2>/dev/null && break
  [ "$i" = 60 ] && { echo "FATAL: scp never became usable on $IP after 5 min"; exit 1; }
  [ "$i" = 1 ] && echo "  scp not ready yet (first-boot wrapper still closing sftp) — retrying..."
  sleep 5
done
ssh root@"$IP" "chmod +x /opt/drill/drain/run_node.sh; ln -sf /opt/drill/drain/run_node.sh /usr/local/bin/run_node.sh"
# Ship ONLY what the GPU box needs. The DO_TOKEN never leaves the laptop.
# This file doubles as the container's --env-file, so: plain KEY=VALUE, no quotes, no export.
SHIP_RE='^(SPACES_[A-Z_]+|CONTROLLER_[A-Z_]+|DRILL_TOKEN|REDIS_PASSWORD|DRILL_CONTAINER|DRILL_IMAGE)='
grep -E "$SHIP_RE" .env | ssh root@"$IP" "cat > /opt/drill/.env"
# Fail loudly here rather than with a KeyError mid-drain upload.
for k in SPACES_KEY SPACES_SECRET SPACES_ENDPOINT SPACES_BUCKET CONTROLLER_IP CONTROLLER_PORT; do
  grep -qE "^$k=.+" <(grep -E "$SHIP_RE" .env) || { echo "FATAL: $k missing/empty in .env"; exit 1; }
done

ssh root@"$IP" \
  SCRATCH_DEV="$SCRATCH_DEV" DRILL_IMAGE="$DRILL_IMAGE" DRILL_CONTAINER="$DRILL_CONTAINER" \
  bash -s <<'REMOTE'
set -euo pipefail

# --- Scratch disk (NEVER the boot disk) -------------------------------------
if ! mountpoint -q /mnt/scratch; then
  ROOT_SRC=$(findmnt -no SOURCE /)
  [ "$SCRATCH_DEV" != "$ROOT_SRC" ] || { echo "FATAL: SCRATCH_DEV is the root device"; exit 1; }
  [ -b "$SCRATCH_DEV" ] || { echo "FATAL: $SCRATCH_DEV is not a block device"; lsblk; exit 1; }
  blkid "$SCRATCH_DEV" >/dev/null 2>&1 || mkfs.ext4 -q "$SCRATCH_DEV"
  mkdir -p /mnt/scratch && mount "$SCRATCH_DEV" /mnt/scratch
fi
mkdir -p /mnt/scratch/ckpt /mnt/scratch/hf /mnt/scratch/logs
: > /mnt/scratch/logs/train.log; : > /mnt/scratch/logs/agent.log
# Logs live on scratch (shared with the container); symlink so the runbook's host-side
# `tail -f /var/log/train.log` keeps working unchanged.
ln -sfn /mnt/scratch/logs/train.log /var/log/train.log
ln -sfn /mnt/scratch/logs/agent.log /var/log/agent.log
echo "scratch: $(df -h /mnt/scratch | tail -1)"

# --- Shut down DO's stock Jupyter container ---------------------------------
# The AMD AI/ML image auto-starts a Jupyter container publishing 8888/8000/30000 on
# 0.0.0.0. It's login-protected but we never use it, and an exposed Jupyter on a public
# GPU box is a mining-bot magnet. Stopping it also frees the ports and ~300 MB of VRAM.
for c in $(docker ps --format '{{.Names}}' | grep -vx "$DRILL_CONTAINER" || true); do
  echo "stopping stock container: $c"
  docker update --restart=no "$c" >/dev/null 2>&1 || true
  docker stop "$c" >/dev/null 2>&1 || true
done

# --- Drill container --------------------------------------------------------
docker image inspect "$DRILL_IMAGE" >/dev/null 2>&1 || {
  echo "FATAL: $DRILL_IMAGE is not on this box. Available:"; docker images; exit 1; }
docker rm -f "$DRILL_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$DRILL_CONTAINER" \
  --network host --uts=host \
  --device=/dev/kfd --device=/dev/dri --group-add video \
  --security-opt seccomp=unconfined --shm-size 16G \
  --env-file /opt/drill/.env -e HF_HOME=/mnt/scratch/hf -e PYTHONUNBUFFERED=1 \
  -v /mnt/scratch:/mnt/scratch -v /opt/drill:/opt/drill \
  -w /opt/drill --restart unless-stopped \
  "$DRILL_IMAGE" sleep infinity >/dev/null
sleep 3

# --uts=host is load-bearing: train.py/drain_agent.py identify the node by os.uname().nodename
# and the drill targets it by droplet name. A default container hostname (the image ID) would
# make the agent poll for a node the drill never names — a silent hang mid-drain.
CN=$(docker exec "$DRILL_CONTAINER" hostname)
[ "$CN" = "$(hostname)" ] || {
  echo "FATAL: container hostname '$CN' != droplet hostname '$(hostname)' — drain targeting would break"
  exit 1; }
echo "container $DRILL_CONTAINER up (hostname $CN)"

# --- Deps, pinned so nothing can swap out the image's ROCm torch -------------
TORCH_V=$(docker exec "$DRILL_CONTAINER" python3 -c 'import torch;print(torch.__version__)')
echo "container torch: $TORCH_V"
docker exec "$DRILL_CONTAINER" bash -c "printf 'torch==%s\n' '$TORCH_V' > /opt/drill/constraints.txt"
docker exec "$DRILL_CONTAINER" pip3 install -q \
  -r /opt/drill/requirements.txt -c /opt/drill/constraints.txt

docker exec -i "$DRILL_CONTAINER" python3 - <<'PY'
import torch
assert torch.cuda.is_available(), "GPU not visible to PyTorch in the drill container"
assert torch.version.hip, "not a ROCm torch"
print("SMOKE OK:", torch.cuda.get_device_name(0),
      "| VRAM:", round(torch.cuda.get_device_properties(0).total_memory/2**30), "GiB",
      "| torch:", torch.__version__, "| hip:", torch.version.hip)
PY

# --- Pre-fetch model + dataset (timed: every reprovision pays this again) ----
T0=$(date +%s)
docker exec -i "$DRILL_CONTAINER" python3 - <<'PY'
from huggingface_hub import snapshot_download
from datasets import load_dataset
snapshot_download("Qwen/Qwen3-8B")
load_dataset("yahma/alpaca-cleaned", split="train")
PY
echo "hf_prefetch_s=$(( $(date +%s) - T0 ))"
echo "hf_cache_size=$(du -sh /mnt/scratch/hf | cut -f1)"
REMOTE

echo "Bootstrap complete on $IP."
echo "Start with: ssh root@$IP 'run_node.sh train' ; ssh root@$IP 'run_node.sh agent'"
