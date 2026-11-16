# AMD MI350X Spot GPU Recovery Drill

Code and operating instructions for a measured checkpoint, reclamation, and resume drill on a DigitalOcean AMD Instinct™ MI350X Spot GPU Droplet.

The drill runs a full fine-tune of Qwen3-8B, sends a simulated reclamation notice, writes the complete training state to local NVMe, uploads it to DigitalOcean Spaces, provisions a replacement Spot GPU Droplet, restores the checkpoint, verifies the data cursor and RNG state, and resumes training.

## Measured reference run

One run was completed on 2026-08-18:

| Stage | Result |
| --- | ---: |
| Full checkpoint | 98.3 GB (91.5 GiB) |
| Write to local NVMe | 56.5 s |
| Upload to same-region Spaces | 264 s at 2.97 Gbps |
| Notice to durable off-box state | 322.6 s (5.4 min) |
| Replacement creation to usable GPU | 201 s |
| Bootstrap | 96 s |
| Checkpoint download | 244 s |
| Load, verify, and resume | about 40 s |
| Resume point | step 14,625 |
| Steps redone | 0 |

This is an `n=1` result. The roughly 15-minute end-to-end recovery figure discussed in the companion video is the sum of individually measured stages, not one uninterrupted stopwatch run.

## What is simulated

DigitalOcean did not provide a way to schedule a real reclamation. The controller therefore sends a simulated notice to the same drain path. Everything after that trigger is executed for real: checkpointing, upload, replacement provisioning, restore, verification, resumed training, and destruction of the old GPU Droplet.

## Architecture

The reasoning behind the design (requirements, each decision and its cost, the measured numbers, the
failure and threat model, what changes for a fleet) is in the [design record](docs/DESIGN.md), with a
decision-flow diagram and a deployment diagram.

```text
operator laptop
    |
    +-- controller Droplet
    |      Flask notice endpoint
    |      Redis (queue_demo only, not used by the drill)
    |      server-timestamped event log
    |
    +-- Spot GPU Droplet A
    |      Qwen3-8B full fine-tune
    |      local NVMe checkpoint
    |      drain agent
    |
    +-- DigitalOcean Spaces
    |      durable checkpoint
    |
    +-- Spot GPU Droplet B
           restore, verify, resume
```

The drill itself (train, drain, upload, restore, resume) does not use Redis. Drill events travel over the Flask notice endpoint, and checkpoints travel through Spaces. Redis is only for the optional `queue_demo/` batch-inference example, which is the sole code that imports `redis`.

Redis is opt-in. `provision/00_controller.sh` installs it only when `ENABLE_REDIS=1` is set (default `0`), and `REDIS_PASSWORD` is required only in that case. When enabled, the script binds Redis to all interfaces (`0.0.0.0`) protected by that password, and opens port 6379 in the controller firewall to your IP and the GPU fleet tag. Keep that firewall in place, and do not enable Redis if you are not running `queue_demo/`. If you provisioned an earlier controller that already has Redis, firewall port 6379 or remove it with `apt-get remove redis-server` on the droplet.

## Cost and destructive-action warning

This project creates billable cloud resources and intentionally destroys GPU Droplets.

- Check current GPU and Spaces pricing before every run.
- Use a dedicated tag, bucket, and scoped Spaces key.
- Keep the operator terminal open until cleanup finishes.
- Confirm the replacement has resumed before destroying the original Droplet.
- Run the teardown checklist even if the drill fails.

The 2026 reference run cost about $20 in GPU time. Current cost and capacity may differ.

## Prerequisites

- A DigitalOcean account with access to AMD Spot GPU Droplets
- `doctl`, authenticated on the operator machine
- Bash, Python 3.10+, `curl`, `ssh`, `scp`, and `openssl`
- A DigitalOcean SSH key
- A private Spaces bucket with a bucket-scoped read/write key
- Sufficient GPU quota and budget
- Access to `Qwen/Qwen3-8B` and `yahma/alpaca-cleaned` on Hugging Face

PyTorch and ROCm run inside the container supplied with the selected AMD AI/ML image. Do not install a different PyTorch build over the image's ROCm build.

## Quick start

```bash
cp .env.example .env
openssl rand -hex 16
openssl rand -hex 16
```

Put the generated values in `DRILL_TOKEN` (and `REDIS_PASSWORD` only if you set `ENABLE_REDIS=1` for `queue_demo/`), complete the remaining configuration, then follow [RUNBOOK.md](RUNBOOK.md).

The main sequence is:

```bash
./provision/00_controller.sh
./provision/01_create_spot.sh drill-node-a
./provision/02_bootstrap.sh "$(cat .state/drill-node-a.ip)"

ssh root@"$(cat .state/drill-node-a.ip)" 'run_node.sh train'
ssh root@"$(cat .state/drill-node-a.ip)" 'run_node.sh agent'

./drain/self_reclaim.sh drill drill-node-a
```

Do not copy these commands blindly. The runbook includes required capacity, image, scratch-device, firewall, and billing checks.

## Repository layout

- `provision/`: controller and GPU lifecycle scripts
- `trainer/`: full fine-tune, checkpoint, upload, timing, and continuity tools
- `drain/`: simulated notice, drain agent, orchestration, and event timeline
- `queue_demo/`: optional lease, acknowledge, and retry example for batch inference
- `docs/`: design record and its diagrams
- `results/`: format and reference measurements from the original run

## Security

Never commit `.env`, `.state/`, credentials, IP addresses, or raw logs. Read [SECURITY.md](SECURITY.md) before running the drill.

## License and trademarks

The repository code is available under the MIT License. Third-party models, datasets, services, and container images retain their own licenses and terms.

AMD, AMD Instinct, DigitalOcean, Droplet, and Spaces are trademarks of their respective owners. This repository documents an independent experiment and does not imply endorsement.
