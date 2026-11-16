# Serving runbook

A paid session that runs vLLM on a DigitalOcean Kubernetes Spot GPU node pool, measures
throughput and latency, tests autoscaling and a simulated reclamation, and tears everything
down. Follow the phases in order. Commands assume the repository root.

**This runbook was executed once, on 2026-11-16.** The results table at the end holds what
that session measured. The tools were first built and tested without a GPU (see
[serving/README.md](serving/README.md)); the session found one manifest bug, fixed since.
Two steps differed from the text below: the sweep was run a second time with `MAX_NUM_SEQS`
raised to 256, because the default of 32 capped throughput, and the load generator needs up to
a minute after `SIGTERM` to write its result file at high concurrency, so wait for the file
instead of sleeping 5 s.

## 0. Read the safety constraints

This session creates billable GPU nodes. Two of them run at once during phases 6 and 7.

1. **Budget: $80** (`SERVING_BUDGET_USD`). `00_preflight.sh` turns that and today's prices
   into a hard stop time. Write the hard stop time down before phase 1.
2. **At the hard stop, go to phase 9 (teardown), whatever phase you are in.** An unfinished
   table is the correct result of a session that ran out of budget.
3. A script failure does not stop billing. Every script that fails prints what is still
   running.
4. Spot capacity is as-available, and the pool can be reclaimed during the session. If it
   is, record it (that is a finding) and go to teardown.
5. The planned session is 160 minutes of allowances (the sum of the phase allowances
   below). They are planning allowances, not measurements. If a phase overruns its
   allowance by half, stop and decide whether the rest still fits.
6. Keep the operator terminal open until the teardown checklist is complete.

What the money buys, as arithmetic on inputs:

```text
worst-case burn  = 2 GPU nodes x (today's Spot price) + 1 CPU node x (its price)
session cost     <= worst-case burn x session hours
```

The only price this repository has ever recorded for the size is a dated snapshot in
`trainer/scale_model.py` (2026-08-24: $32.00 per hour for the 8-GPU MI350X box, which that
file treats as 8 times the single-GPU price). Spot prices move daily. Use the preflight's
live figure, not that one.

## 1. Preflight (free, creates nothing)

Add the serving settings to `.env` (see the serving section of `.env.example`). At minimum
`SERVING_CPU_SIZE`, which has no default.

```bash
serving/cluster/00_preflight.sh 160
```

It prints the DOKS versions, whether the region and sizes are offered for node pools,
today's prices, the worst-case burn, and whether a 160 minute session fits the budget.

Record:

- GPU price per hour and CPU price per hour, with the date
- hard stop: minutes from start, and the clock time once you start

Stop here if it prints `PREFLIGHT FAILED`, if the session does not fit, or if the Spot size
is not offered. The fallbacks are in serving/README.md, decision S1. Also check the Spot GPU
capacity page linked from the DOKS supported GPUs documentation.

Run the offline checks once more on the commit you are about to use:

```bash
python3 -m unittest discover -s serving/tests -t .
serving/validate_manifests.sh
```

## 2. Create the cluster (allowance 20 min, 1 GPU node billing)

```bash
serving/cluster/01_create_cluster.sh --confirm
```

Billing starts when the first node is ready. **Start your timer now.** The script creates a
CPU pool and a Spot GPU pool with autoscaling from `SERVING_GPU_MIN` to `SERVING_GPU_MAX`
(1 to 2). That range cannot be raised later. It waits, with bounds, for a Ready GPU node
that advertises `amd.com/gpu`.

Record (the script prints them and appends them to `.state/serving-events.jsonl`):

- create to cluster running
- create to GPU node Ready
- create to GPU allocatable
- the locked GPU price in `.state/serving-gpu-price-hourly`

Gate: the node list the script prints must show
`doks.digitalocean.com/capacity-type=spot` on the GPU node. If it does not, you are not on
spot. Stop and tear down.

## 3. Install and first cold start (allowance 30 min)

Hard gate first: check the node before putting a model on it.

```bash
kubectl get nodes -L doks.digitalocean.com/gpu-model,doks.digitalocean.com/capacity-type
kubectl get nodes -l doks.digitalocean.com/gpu-brand=amd \
  -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.amd\.com/gpu,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory,DISK:.status.allocatable.ephemeral-storage'
```

Record the allocatable CPU, memory and disk. The model cache is a directory on the node's
disk; if `DISK` is not comfortably larger than the model, stop and change the cache volume.
If CPU or memory are below the requests in `serving/k8s/vllm-deployment.yaml`, lower the
requests.

For a gated model only, create the token secret by hand (never in Git):

```bash
kubectl create namespace vllm
kubectl -n vllm create secret generic hf-token --from-literal=token="$HF_TOKEN"
```

Then:

```bash
serving/cluster/02_install.sh
```

It installs KEDA, applies `serving/k8s`, and waits at most 35 minutes for the first vLLM pod
to be Ready. That wait covers the image pull, the model download and the model load.

Record:

- apply to pod scheduled
- apply to pod Ready (the first cold start)
- the image tag that worked

If the pod never becomes Ready, look before anything else:

```bash
kubectl -n vllm describe pod -l app=vllm | tail -40
kubectl -n vllm logs -l app=vllm --tail=100
```

The most likely failure is the one fact that could not be verified in advance: the ROCm
build in the image against the node's kernel driver. Try one other tag (change `newTag` in
`serving/k8s/kustomization.yaml`, then `kubectl apply -k serving/k8s`). The tags are listed
on Docker Hub; vLLM's documentation describes `latest` (ROCm 10.0) and the legacy
`latest-rocm72`. Allow two attempts. If both fail, record the error text and go to teardown.
That is a result.

## 4. Smoke test (allowance 10 min)

The commands from here on assume bash (they rely on `$LG` splitting into words).

```bash
LG="kubectl -n vllm exec deploy/loadgen --"
mkdir -p results/serving

$LG python3 /opt/loadgen/loadgen.py --base-url http://vllm:8000 --model qwen3-8b \
  --concurrency 1 --duration 30 --max-tokens 128 \
  --hardware "1x MI350X spot, DOKS" --out /work/smoke.json
$LG cat /work/smoke.json > results/serving/smoke.json
python3 -c 'import json; r=json.load(open("results/serving/smoke.json")); print(r["is_fake_server"], r["levels"][0]["requests_ok"], r["levels"][0]["token_count_source"])'
```

Gate: `False`, a non-zero count, and `usage`. If the token source is `chunks`, the server is
not sending usage and token counts are approximate; note it next to every number.

Check the metrics the autoscaler will read:

```bash
kubectl get --raw '/api/v1/namespaces/vllm/services/prometheus:9090/proxy/api/v1/query?query=vllm:num_requests_running' | head -c 400; echo
kubectl -n vllm get scaledobject,hpa
```

Gate: the query returns a series for the vLLM pod, and the ScaledObject shows `READY True`.

Qwen3 produces thinking text by default. It counts as output tokens here because the GPU
generated it. If you want answers only, say so in the request with `--extra-body` and
record that you did; check the model card for the exact switch.

## 5. Single-replica sweep (allowance 20 min)

Hold the replica count at 1 so the autoscaler does not move during the measurement:

```bash
kubectl -n vllm patch scaledobject vllm --type merge -p '{"spec":{"maxReplicaCount":1}}'

$LG python3 /opt/loadgen/loadgen.py --base-url http://vllm:8000 --model qwen3-8b \
  --concurrency 1,2,4,8,16,32,64 --duration 120 --pause 10 --max-tokens 256 \
  --hardware "1x MI350X spot, DOKS, 1 replica" --out /work/sweep.json
$LG cat /work/sweep.json > results/serving/sweep.json
```

Seven levels of 120 s is about 15 minutes. Levels above `MAX_NUM_SEQS` (32 by default) queue
inside vLLM on purpose: they show what waiting looks like.

Record, per level, from `results/serving/sweep.json`:

- output tokens per second for the level
- time to first token p50, p95, p99
- time per output token p50, p95
- failed requests

Cost per million tokens, using the price locked at pool creation:

```bash
python3 serving/cost_per_mtok.py results/serving/sweep.json \
  --price-hourly "$(cat .state/serving-gpu-price-hourly)" \
  --price-source "Spot rate at pool creation, $(cat .state/serving-started-at)" \
  --gpu-nodes 1 --overhead-hourly "$(cat .state/serving-cpu-price-hourly)" \
  --out results/serving/cost.json
```

Add `--target-per-mtok X` to see the utilisation at which a target price is met.

Now choose the two autoscaling inputs from what you measured. Pick the highest concurrency
whose time to first token is still acceptable to you; call it C.

- `MAX_NUM_SEQS` = C. Requests beyond it wait in the queue.
- `TARGET_INFLIGHT` = about three quarters of C, so a second replica is requested before the
  first one is full.

Edit both in `serving/k8s/kustomization.yaml`, then apply and restore the replica range:

```bash
kubectl apply -k serving/k8s
kubectl -n vllm rollout status deployment/vllm --timeout=20m
```

The rollout restarts the pod, so the model loads again, this time from the node's cache.
Record that time as the warm restart. Record the values of C, `MAX_NUM_SEQS` and
`TARGET_INFLIGHT` you chose, and export the two the next phases use:

```bash
export C=...                 # the concurrency you picked
export TARGET_INFLIGHT=...   # the value you put in kustomization.yaml
```

## 6. Autoscaling test (allowance 30 min, 2 GPU nodes billing)

The question: from the moment the queue builds up, how long until a new replica is serving?
That interval contains the autoscaler's decision, a new Spot GPU node, the image pull, the
model download and the model load.

Terminal A, on the laptop, records cluster changes on one clock:

```bash
python3 serving/scale_watch.py watch --inflight-threshold "$TARGET_INFLIGHT" \
  --out results/serving/autoscale-events.jsonl
```

Terminal B starts a load of about twice the target and keeps it there. It runs in the
background inside the pod so a dropped laptop connection does not end it:

```bash
$LG sh -c 'nohup python3 /opt/loadgen/loadgen.py --base-url http://vllm:8000 --model qwen3-8b \
  --concurrency '"$((TARGET_INFLIGHT * 2))"' --duration 1500 --max-tokens 256 --timeline-bucket 10 \
  --hardware "MI350X spot, DOKS, autoscaling 1 to 2" --out /work/autoscale.json \
  > /work/autoscale.log 2>&1 & echo $! > /work/autoscale.pid'
```

Watch terminal A for, in order: `inflight_above_threshold`, `replicas_wanted_changed`,
`node_seen`, `node_ready`, `pod_scheduled`, `pod_ready`. Give up after 25 minutes with no new
Ready pod: record "no Spot capacity for a second node within 25 min" and move on.

Before stopping the load, pin two replicas so the next phase has them:

```bash
kubectl -n vllm patch scaledobject vllm --type merge -p '{"spec":{"minReplicaCount":2}}'
```

Let the load run two more minutes with both replicas serving, then stop it and collect:

```bash
$LG sh -c 'kill -TERM "$(cat /work/autoscale.pid)"; sleep 5; tail -3 /work/autoscale.log'
$LG cat /work/autoscale.json > results/serving/autoscale.json
python3 serving/scale_watch.py summary results/serving/autoscale-events.jsonl
```

Leave terminal A running for the next phase, or restart it with a new `--out`.

Record, from the summary:

- queue build-up to autoscaler asking for a second replica
- to new node seen, and to new node Ready (GPU node provisioning)
- to new pod scheduled
- to new pod Ready (a new replica serving). This is the headline number
- output tokens per second before and after, from the 10 s timeline in
  `results/serving/autoscale.json`

Resolution is the watch interval (3 s) plus the Prometheus scrape (5 s).

## 7. Reclamation test for serving (allowance 25 min, 2 GPU nodes billing)

**This is a simulated reclamation**, as in the training drill. DigitalOcean offers no way to
schedule a real one. A real reclamation takes the whole Spot pool; DOKS cordons and drains
its nodes and respects PodDisruptionBudgets. `kubectl drain` goes through the same eviction
path for one node. What is real: the eviction, the endpoint removal, the `preStop` sleep,
SIGTERM, vLLM's shutdown, and every request that fails or is retried.

Needs two Ready replicas (`kubectl -n vllm get pods -l app=vllm -o wide`).

Run it twice: once with no client retries to see what a naive client loses, once with
retries to see what a reasonable client loses. Use a concurrency that one replica can carry
(C from phase 5 or lower), because after the drain one replica has all of it.

```bash
drain_run () {   # $1 = run name   $2 = retries
  NODE=$(kubectl -n vllm get pods -l app=vllm -o jsonpath='{.items[0].spec.nodeName}')
  $LG sh -c 'nohup python3 /opt/loadgen/loadgen.py --base-url http://vllm:8000 --model qwen3-8b \
    --concurrency '"$C"' --duration 600 --max-tokens 256 --timeline-bucket 5 --save-requests \
    --retries '"$2"' --hardware "MI350X spot, DOKS, 2 replicas, drain of one node" \
    --note "simulated reclamation: kubectl drain" --out /work/'"$1"'.json \
    > /work/'"$1"'.log 2>&1 & echo $! > /work/'"$1"'.pid'
  sleep 60                                   # steady state before the drain
  date -u +%s > "results/serving/$1.drain-started"
  kubectl drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=240s
  date -u +%s > "results/serving/$1.drain-finished"
  sleep 60                                   # one replica carrying the load
  $LG sh -c 'kill -TERM "$(cat /work/'"$1"'.pid)"; sleep 5'
  $LG cat "/work/$1.json" > "results/serving/$1.json"
  kubectl uncordon "$NODE"
  kubectl -n vllm rollout status deployment/vllm --timeout=20m   # second replica back, warm cache
}

drain_run drain-noretry 0
drain_run drain-retry 3
```

Record, for each run, from `results/serving/drain-*.json`:

- `requests_failed`, with the error kinds
- `requests_retried` and `requests_ok_after_retry`
- drain duration (finished minus started)
- from the timeline: output tokens per second before, during and after the drain
- from `scale_watch`: `pod_terminating` to `pod_gone`, and after the uncordon,
  `pod_scheduled` to `pod_ready` (the warm restart on a node that still has the weights)

If requests fail with `stream_truncated` or `connection_*` errors during the drain, the pod
stopped before finishing them. Raise the `preStop` sleep in
`serving/k8s/vllm-deployment.yaml` to cover the longest request (and the grace period with
it), apply, and repeat one run if the budget allows. Record both results.

Not covered by this test: a reclamation with no notice, and the loss of the whole pool.

## 8. Optional: two-replica throughput (allowance 5 min)

Only if you are ahead of the hard stop.

```bash
$LG python3 /opt/loadgen/loadgen.py --base-url http://vllm:8000 --model qwen3-8b \
  --concurrency "$((C * 2))" --duration 120 --max-tokens 256 \
  --hardware "2x MI350X spot, DOKS, 2 replicas" --out /work/two.json
$LG cat /work/two.json > results/serving/two.json
python3 serving/cost_per_mtok.py results/serving/two.json --gpu-nodes 2 \
  --price-hourly "$(cat .state/serving-gpu-price-hourly)" \
  --price-source "Spot rate at pool creation" \
  --overhead-hourly "$(cat .state/serving-cpu-price-hourly)"
```

## 9. Teardown (allowance 20 min)

Do every line, in order, even if an earlier phase failed.

1. Copy out anything still in the cluster. The destroy step does not.

   ```bash
   $LG sh -c 'ls -la /work'
   kubectl -n vllm get scaledobject,hpa,pods,pdb -o wide > results/serving/final-state.txt
   kubectl -n vllm logs -l app=vllm --tail=200 > results/serving/vllm-tail.log
   cp .state/serving-events.jsonl results/serving/
   ```

2. Destroy the cluster and what it created:

   ```bash
   serving/cluster/03_destroy_cluster.sh --confirm
   ```

   It deletes the cluster with its volumes and load balancers, then checks for up to five
   minutes that the cluster and its Droplets are gone. If it prints `STILL PRESENT`, deal
   with that before anything else.

3. Check by hand that nothing is left billing:

   ```bash
   doctl kubernetes cluster list
   doctl compute droplet list --format ID,Name,SizeSlug,Status | grep -i gpu
   doctl compute volume list
   doctl compute load-balancer list
   ```

   All four should show nothing from this session.

4. Record the session start and end (`.state/serving-started-at`,
   `.state/serving-destroyed-at`) and compute the upper bound on cost:
   hours times worst-case burn. The invoice is the record of what it actually cost; check
   the billing page the next day and write that number down too.

5. If you created an `hf-token` secret, it went with the cluster. Rotate the token if it
   appeared in a terminal or a log.

6. Before publishing anything from `results/serving/`: remove node names, cluster IDs, IP
   addresses and account details. `results/` is ignored by Git for that reason. Only the
   table below is published.

## Results

Measured in one session on 2026-11-16. One session is `n = 1`. Nothing here is from the fake
server, a projection or another GPU. Raw files stay out of Git (`results/serving/`).

Session facts:

| Item | Value |
|------|-------|
| Date | 2026-11-16, 14:07 to 15:18 UTC (71 minutes from create to destroy) |
| Region, DOKS version | `ric1`, 1.37.2-do.0 |
| GPU node size, Spot price per hour at pool creation | `gpu-mi350x1-288gb-spot`, $3.00. CPU node `s-4vcpu-8gb-intel`, $0.083 |
| vLLM image tag, model, `MAX_MODEL_LEN`, `MAX_NUM_SEQS` | `vllm/vllm-openai-rocm:v0.33.0` (the first tag tried), Qwen/Qwen3-8B, 8192, 32 then 256 for the sweeps, 128 for the autoscaling and drain tests |
| Prompt set, `max_tokens`, thinking on or off | the load generator's 8 built-in prompts, 256, thinking on (the model default), temperature 0 |
| Session length, cost upper bound, invoiced cost | 71 minutes. About $4.20 by node-minutes at the posted rates (81 GPU node-minutes, 65 CPU node-minutes); $7.20 at the worst-case burn for the whole session. Invoice not yet checked |

Provisioning and cold start:

| Stage | Result |
|-------|-------:|
| Create to cluster running | 406 s |
| Create to GPU node Ready | 407 s |
| Create to GPU allocatable | 408 s |
| Image pull on a new node (18.0 GB) | 101.5 s |
| Weights download (15.3 GiB) | 13.4 s |
| Model load | 17.2 s |
| Cold start on a new node: pod scheduled to Ready | 235 s |
| Warm restart: pod scheduled to Ready, weights cached | 102 s and 97 s |
| Config rollout on one node (old pod drained, new pod Ready) | 123 s and 128 s |

The first cold start from `kubectl apply` was not timed cleanly: the first pod crash-looped on
the `VLLM_PORT` service-link variable (fixed in `vllm-deployment.yaml` during the session). The
cold start figure is the second replica's, on a node that had never run the image.

Single replica, one MI350X, 120 s per level (100 s for the last three):

| Concurrency | `MAX_NUM_SEQS` | Output tokens/s | TTFT p50 | TTFT p95 | TTFT p99 | Time per output token p50 | Failed | $ per 1M output tokens | $ per 1M total tokens |
|------------:|---------------:|----------------:|---------:|---------:|---------:|--------------------------:|-------:|-----------------------:|----------------------:|
| 1 | 32 | 261 | 11 ms | 11 ms | 11 ms | 3.8 ms | 0 | $3.288 | $3.059 |
| 2 | 32 | 527 | 16 ms | 17 ms | 18 ms | 3.7 ms | 0 | $1.624 | $1.511 |
| 4 | 32 | 920 | 14 ms | 18 ms | 20 ms | 4.3 ms | 0 | $0.931 | $0.867 |
| 8 | 32 | 1,718 | 17 ms | 19 ms | 21 ms | 4.6 ms | 0 | $0.499 | $0.464 |
| 16 | 32 | 3,287 | 17 ms | 22 ms | 31 ms | 4.8 ms | 0 | $0.261 | $0.242 |
| 32 | 32 | 6,244 | 22 ms | 33 ms | 40 ms | 5.1 ms | 0 | $0.137 | $0.128 |
| 64 (queued, limit 32) | 32 | 6,404 | 1298 ms | 1332 ms | 1341 ms | 4.9 ms | 0 | $0.134 | $0.124 |
| 64 | 256 | 10,566 | 27 ms | 37 ms | 65 ms | 6.0 ms | 0 | $0.081 | $0.075 |
| 128 | 256 | 16,423 | 37 ms | 60 ms | 103 ms | 7.7 ms | 0 | $0.052 | $0.049 |
| 256 | 256 | 22,604 | 91 ms | 156 ms | 225 ms | 11.0 ms | 0 | $0.038 | $0.035 |

Cost per million tokens is at 100% utilisation of that level, GPU node plus CPU pool. At half
that utilisation the cost doubles. Output tokens include the model's thinking text.

With the default batch limit of 32, throughput stopped at about 6,400 output tokens per second
and requests beyond the limit waited 1.3 s for a first token. That was the limit, not the GPU.
With the limit at 256 throughput was still rising at 256 concurrent requests, so the GPU's
ceiling for this model and prompt length was not reached.

Autoscaling, 1 to 2 replicas, timed from in-flight requests first reaching the target (96 per
replica, load of 192 concurrent requests):

| Milestone | Seconds |
|-----------|--------:|
| Autoscaler asks for a second replica | 11 |
| New GPU node seen | 182 |
| New GPU node Ready | 192 |
| New pod scheduled | 220 |
| **New replica serving (pod Ready)** | **455** |
| Output tokens/s before and after | 16,384 and 26,470 |

No request failed during the test (42,073 completed). Requests over the batch limit queued:
median time to first token for the whole run was 154 ms. Resolution is about 8 s.

Simulated reclamation, drain of one of two nodes under a load of 96 concurrent requests:

| Measure | No client retries | 3 client retries |
|---------|------------------:|-----------------:|
| Requests sent | 8,352 | 8,352 |
| Requests failed | 0 | 0 |
| Requests retried, and recovered | 0, 0 | 0, 0 |
| Error kinds | none | none |
| Drain duration | 22 s | 23 s |
| Output tokens/s, median and lowest 5 s bucket | 14,746 and 12,134 | 14,746 and 11,213 |
| Evicted pod: terminating to gone | 22 s | 21 s |

Read the zero with its limits. Requests here last about two seconds, so every in-flight request
finished inside the 15 s `preStop` sleep and vLLM's 120 s shutdown window was never needed. A
workload with long generations would test that window; this one did not. The retry run made no
retries because nothing failed, so it shows nothing the first run does not.

Not measured by this runbook even when complete: a real reclamation, loss of the whole Spot
pool, reclamation without notice, scale-in, failover to an on-demand pool, latency from
outside the cluster, a workload with long prompts, and any repeat. One session is `n = 1`.
