# Reproduction runbook

Follow the phases in order. Commands assume the repository root as the working directory.

## 0. Read the safety constraints

This drill creates billable resources and destroys the original GPU Droplet after a replacement resumes successfully.

Before starting:

1. Set a budget and a hard stop time.
2. Use a unique `DRILL_TAG`.
3. Use a new, private Spaces bucket and a bucket-scoped key.
4. Keep `.env` and `.state/` out of Git.
5. Plan to delete GPU Droplets, the controller, firewalls, tags, Spaces objects, and access keys.

Spot GPU Droplets are capacity-dependent. A script failure does not stop billing.

## 1. Install and authenticate `doctl`

Install `doctl` using DigitalOcean's current instructions, then authenticate:

```bash
doctl auth init
doctl account get
doctl compute ssh-key list
```

Put the numeric SSH key ID in `.env`.

## 2. Check current capacity, regions, images, and prices

Do not rely on the reference run's availability or rates:

```bash
doctl compute size list -o json | python3 -c '
import json, sys
for size in json.load(sys.stdin):
    if "mi35" in size["slug"]:
        print(size["slug"], size.get("price_hourly"), size.get("regions"))
'

doctl compute image list --public --format Slug,Name | grep -Ei 'amd|rocm|pytorch'
```

Update `SPOT_SIZE_SLUG`, `REGION`, `FALLBACK_SIZE_SLUG`, `FALLBACK_REGION`, and `IMAGE_SLUG` in `.env`.

The reference run used MI350X in `ric1`; MI355X in `mem1` was the fallback. Availability can change.

## 3. Create the Spaces bucket and scoped key

Create a private bucket in the same region as the primary GPU Droplet. Disable CDN access and restrict file listing.

Create a bucket-scoped read/write key:

```bash
doctl spaces keys create spot-drill \
  --grants 'bucket=YOUR_BUCKET_NAME;permission=readwrite'
```

The secret is shown once. Save it directly into `.env`, never into shell history or Git.

Verify access from a temporary local Python environment:

```bash
python3 -m venv .preflight-venv
. .preflight-venv/bin/activate
pip install boto3
set -a; . ./.env; set +a
python3 - <<'PY'
import os
import boto3

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["SPACES_ENDPOINT"],
    aws_access_key_id=os.environ["SPACES_KEY"],
    aws_secret_access_key=os.environ["SPACES_SECRET"],
)
bucket = os.environ["SPACES_BUCKET"]
s3.head_bucket(Bucket=bucket)
s3.put_object(Bucket=bucket, Key="_preflight/probe", Body=b"ok")
assert s3.get_object(Bucket=bucket, Key="_preflight/probe")["Body"].read() == b"ok"
s3.delete_object(Bucket=bucket, Key="_preflight/probe")
print("Spaces preflight passed")
PY
deactivate
```

## 4. Complete `.env`

```bash
cp .env.example .env
openssl rand -hex 16
openssl rand -hex 16
```

Use the generated value for `DRILL_TOKEN`. `REDIS_PASSWORD` is needed only if you set `ENABLE_REDIS=1` for `queue_demo/`.

Required values:

- `DO_TOKEN`
- `SSH_KEY_ID`
- primary and fallback size/region pairs
- `IMAGE_SLUG`
- `SPACES_KEY`, `SPACES_SECRET`, `SPACES_ENDPOINT`, `SPACES_BUCKET`
- `DRILL_TOKEN`, `REDIS_PASSWORD`
- `DRILL_IMAGE`, after inspecting the selected AMD image
- `SCRATCH_DEV`, after the manual device check below

## 5. Create and verify the controller

```bash
./provision/00_controller.sh
```

The script:

- creates a small controller Droplet
- installs Redis and the Flask notice endpoint
- creates tag-scoped firewalls
- writes `CONTROLLER_IP` into `.env`
- checks token and Redis authentication

If your public IP changes during the experiment, update the firewall before continuing.

## 6. Create the first Spot GPU Droplet

```bash
./provision/01_create_spot.sh drill-node-a
```

The script writes:

```text
.state/drill-node-a.id
.state/drill-node-a.ip
```

Verify in the control panel that the requested Droplet uses the spot billing tier.

## 7. Hard gate: inspect the image and scratch device

Connect to the new Droplet:

```bash
IP="$(cat .state/drill-node-a.ip)"
ssh root@"$IP"
```

Inspect the system:

```bash
lsblk
findmnt -no SOURCE /
docker images
docker ps
PATH="$PATH:/opt/rocm/bin" rocm-smi --showproductname
```

Identify the unmounted scratch device. Confirm that it is not the root device or a parent of the root partition. Put the exact device path in `SCRATCH_DEV`.

Confirm the `rocm/pytorch` image tag that exposes the GPU and put it in `DRILL_IMAGE`.

Exit the Droplet, then bootstrap:

```bash
./provision/02_bootstrap.sh "$IP"
```

Bootstrap formats `SCRATCH_DEV` only after checking that it is a block device and is not the root filesystem. A wrong value can destroy data.

## 8. Start training and the drain agent

```bash
ssh root@"$IP" 'run_node.sh train'
ssh root@"$IP" 'run_node.sh agent'
ssh root@"$IP" 'run_node.sh status'
ssh root@"$IP" 'tail -f /var/log/train.log'
```

Wait until training has completed enough steps for a meaningful resume point.

Optional checkpoint timing sample:

```bash
ssh root@"$IP" 'run_node.sh ckpt'
ssh root@"$IP" 'tail -f /var/log/train.log'
```

Upload a completed manual checkpoint:

```bash
ssh root@"$IP" 'run_node.sh push /mnt/scratch/ckpt/stepNNNNNNN'
```

Repeat if you want a median rather than one timing.

## 9. Run the reclamation drill

```bash
./drain/self_reclaim.sh drill drill-node-a
```

The script performs this sequence:

1. reset the controller event log for a new run
2. send a simulated notice targeted at the original node
3. signal the trainer to finish its current step
4. write and upload a complete checkpoint
5. wait until the controller records `node_safe`
6. create a replacement before destroying the original
7. try the configured fallback region if primary capacity is unavailable
8. bootstrap the replacement
9. download the checkpoint
10. resume for 100 additional steps
11. verify the data cursor and RNG state
12. copy local timing/loss records from the original
13. destroy the original Droplet

If any step fails, the trap prints the still-billing Droplets. Investigate or tear them down immediately.

## 10. Inspect the results

Render the current controller timeline:

```bash
set -a; . ./.env; set +a
python3 drain/timeline.py
```

On the active GPU Droplet:

```bash
ssh root@"$(cat .state/DRILL_REPLACEMENT_NAME.ip)" \
  'docker exec drill python3 /opt/drill/trainer/timings.py'
```

To test loss continuity, resume the same checkpoint multiple times for a small fixed number of steps, then run:

```bash
ssh root@"REPLACEMENT_IP" \
  'docker exec drill python3 /opt/drill/trainer/continuity.py CHECKPOINT_STEP 5'
```

`RESUME OK` verifies the stored cursor and RNG fingerprints. Small divergence after the first resumed step can still occur from nondeterministic GPU reductions; compare multiple replays before attributing it to missing checkpoint state.

## 11. Optional larger-model projection

This uses the reference run's measured byte and transfer rates:

```bash
python3 trainer/scale_model.py
python3 trainer/scale_model.py --crossover
```

The 70B path is a projection. A sharded multi-GPU checkpoint is a different code path from the single-GPU `torch.save` used by this drill.

## 12. Teardown

Export controller events and destroy all GPU Droplets carrying the drill tag:

```bash
./provision/03_destroy.sh --all --confirm
```

Confirm no GPU Droplets remain:

```bash
set -a; . ./.env; set +a
doctl compute droplet list --tag-name "$DRILL_TAG"
```

Then delete the controller, associated firewalls/tags, checkpoint objects and bucket, and scoped Spaces key using the DigitalOcean control panel or current `doctl` commands.

Rotate any credential that may have appeared in logs or terminal history.
