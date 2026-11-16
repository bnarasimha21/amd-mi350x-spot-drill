# Serving half

vLLM on DigitalOcean Kubernetes with autoscaling, a load generator, and a cost per million
tokens calculator.

**Status: built and tested without a GPU. Not run on a GPU. There are no serving numbers.**
Nothing in this directory has been applied to a cluster. The manifests are schema-valid, the
tools pass their tests against a fake server, and the cluster scripts have only been run
against stub `doctl` and `kubectl` binaries. The paid session that produces numbers is
[SERVING.md](../SERVING.md).

## What is here

| Path | What it is | How it was tested |
|------|------------|-------------------|
| `k8s/` | kustomize manifests: vLLM Deployment, Service, PodDisruptionBudget, KEDA ScaledObject, a small Prometheus, a load generator pod | `validate_manifests.sh`: rendered, then checked against Kubernetes and KEDA schemas with kubeconform |
| `cluster/00_preflight.sh` | Read-only check of versions, sizes, prices and budget | `bash -n` only. Needs a real account |
| `cluster/01_create_cluster.sh` | Creates the cluster and times it | Stub `doctl` and `kubectl`: guards and the exact create command |
| `cluster/02_install.sh` | Installs KEDA and the manifests, times the first cold start | Stub: refuses the wrong kubectl context |
| `cluster/03_destroy_cluster.sh` | Deletes the cluster and checks for leftovers | Stub: delete, then leftover check |
| `loadgen.py` | Streaming load generator, standard library only | End to end against the fake server |
| `fake_openai_server.py` | **Fake** OpenAI-compatible server, for tests only | It is the test fixture |
| `cost_per_mtok.py` | Cost per million tokens from a result file and a price | Hand-checkable arithmetic |
| `pricing.py` | Live price lookup through `doctl`, no built-in prices | Parser tested on a fixture |
| `budget.py` | Budget and prices in, hard stop time out | Hand-checkable arithmetic |
| `scale_watch.py` | Records cluster changes on one clock during the tests | Diffing and intervals on hand-written snapshots |
| `selftest.sh` | Runs loadgen and the calculator against the fake server | Run in CI |
| `tests/` | 41 unit tests, standard library `unittest` | Run in CI |

Run everything that can run offline:

```bash
python3 -m unittest discover -s serving/tests -t .
serving/selftest.sh
serving/validate_manifests.sh     # needs kubectl (or kustomize) and kubeconform
```

## Platform facts

Checked on 2026-11-16 against the sources listed. Preview products change, so
`cluster/00_preflight.sh` asks the live API again before any money is spent.

### Verified from primary sources

| Fact | Source |
|------|--------|
| vLLM supports AMD ROCm GPUs. Instinct MI350 (gfx950) is listed and needs ROCm 7.0 or newer | vLLM `docs/getting_started/installation/gpu.rocm.inc.md` [1] |
| The official ROCm image is `vllm/vllm-openai-rocm` on Docker Hub. Tag `v0.33.0` exists, pushed 2026-11-02. `latest` is built on ROCm 10.0 | [1], Docker Hub tag list [2] |
| The image takes the server arguments directly (`... vllm/vllm-openai-rocm:<tag> --model <id>`), serves on port 8000, and the documented `docker run` uses `--device /dev/kfd --device /dev/dri --group-add video --ipc=host` | [1] |
| vLLM exposes the gauges `vllm:num_requests_running` ("Number of requests in model execution batches") and `vllm:num_requests_waiting` ("Number of requests waiting to be processed"), labelled `model_name` and `engine`, plus `vllm:kv_cache_usage_perc`, `vllm:generation_tokens`, `vllm:prompt_tokens` and `vllm:time_to_first_token_seconds` | vLLM source at tag v0.33.0, `vllm/v1/metrics/loggers.py` [3] |
| `/metrics` is served by the API server itself. `/health` returns 200 when the engine is healthy and 503 when it is dead | `vllm/entrypoints/serve/instrumentator/metrics.py`, `health.py` at v0.33.0 [3] |
| `vllm serve` has `--shutdown-timeout`: "Shutdown timeout in seconds. 0 = abort, >0 = wait." The default is 0 | `vllm/engine/arg_utils.py` at v0.33.0 [3] |
| DOKS offers AMD GPU node pools. On-demand: `gpu-mi300x1-192gb`, `gpu-mi300x8-1536gb`. MI325X and on-demand MI350X are by contract only | DOKS supported GPUs page [4] |
| DOKS offers **Spot GPU node pools** in public preview, including `gpu-mi350x1-288gb-spot` and `gpu-mi355x1-288gb-spot`, in ATL1, RIC1 and MKC1, with MEM1 as early access | [4] |
| A Spot GPU pool is reclaimed as a whole pool. Notice is best effort, at least two hours, by email, and can be shorter or absent. DOKS cordons and drains the nodes and respects PodDisruptionBudgets | [4] |
| After creation a Spot GPU pool's node count and autoscaling maximum cannot be raised. The autoscaler can still add nodes inside the range set at creation, when Spot capacity exists | [4], DOKS autoscaling page [5] |
| The Spot rate is locked when the pool is created and applies to nodes added later by autoscaling. GPU worker nodes bill per second at the GPU Droplet rate. The control plane is free unless highly available | DOKS pricing page [6] |
| A cluster with a GPU pool needs at least one CPU pool. Failover from a Spot pool to an on-demand pool is not automatic | [4], [5] |
| GPU access: the AMD ROCm device plugin (`--enable-amd-gpu-device-plugin` in doctl) or the AMD DRA driver, one or the other. From DOKS 1.37.0 neither is on by default. With either, workloads can request `amd.com/gpu` | [4], `doctl kubernetes cluster create --help` (doctl v1.186.0, built locally) |
| DOKS taints AMD GPU nodes `amd.com/gpu:NoSchedule` and labels them `doks.digitalocean.com/gpu-brand: amd`. Spot nodes carry `doks.digitalocean.com/capacity-type=spot` and get no extra taint | DOKS managed elements page [7], [4] |
| The cluster autoscaler can scale a GPU pool to zero. doctl node pool syntax includes `auto-scale`, `min-nodes`, `max-nodes` | [5], doctl help |
| KEDA installs with `kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v<version>/keda-<version>.yaml`. The 2.21.1 manifest downloads, and its Deployments are `keda-operator`, `keda-metrics-apiserver` and `keda-admission` in namespace `keda` (read from the 2.21.0 and 2.21.1 manifests) | KEDA deploy docs [8], the release files |
| The Prometheus scrape configuration in `k8s/prometheus.yaml` is valid | `promtool check config` from Prometheus 3.16.0, run by hand once |

### Settled by the session of 2026-11-16

These could only be settled on real hardware. Each was open when the code was written.

| Question | Answer |
|----------|--------|
| Does `vllm/vllm-openai-rocm:v0.33.0` run on a DOKS MI350X node? | Yes, the first tag tried. The node ran kernel 6.12 on Debian 13; vLLM saw 287.69 GiB on the device |
| Does the pod start as written? | No. The Service is named `vllm`, so Kubernetes injected `VLLM_PORT=tcp://...`, which vLLM reads as its own setting and rejects. Fixed with `enableServiceLinks: false` |
| Does the drain lose requests? | 0 of 8,352 failed in each of two runs. Requests lasted about two seconds, so all finished inside the `preStop` sleep. `--shutdown-timeout` was not needed and so is still untested against long streams |
| Hourly prices | $3.00 for `gpu-mi350x1-288gb-spot`, $0.083 for `s-4vcpu-8gb-intel`, on the day |
| Which CPU size | `s-4vcpu-8gb-intel` is offered to DOKS in `ric1` and carried KEDA, Prometheus and a load generator driving 256 streams |
| Does doctl accept the Spot slug in `--node-pool`, and was there capacity | Yes, for the first node and for a second one 3 minutes after the autoscaler asked |
| The `gpu-model` label on MI350X nodes | `mi350x` |
| Free disk on a GPU node | 684 GB allocatable ephemeral storage |
| Node join, download and load times | In the SERVING.md results: 408 s create to GPU allocatable, 235 s cold start on a new node |
| Does the model fit with room for a batch | 15.3 GiB of weights; vLLM reported KV cache for about 218 concurrent 8,192-token requests |
| Does KEDA's Prometheus trigger work on real vLLM metrics | Yes. It asked for a second replica 11 s after in-flight requests passed the target |

Still not verified: a real reclamation, loss of the whole pool, long-running requests during a
drain, scale-in, and any repeat of the session.

Sources:

1. https://github.com/vllm-project/vllm/blob/main/docs/getting_started/installation/gpu.rocm.inc.md
   (rendered at https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)
2. https://hub.docker.com/r/vllm/vllm-openai-rocm/tags
3. https://github.com/vllm-project/vllm/tree/v0.33.0
4. https://docs.digitalocean.com/products/kubernetes/details/supported-gpus/
5. https://docs.digitalocean.com/products/kubernetes/how-to/autoscale/
6. https://docs.digitalocean.com/products/kubernetes/details/pricing/
7. https://docs.digitalocean.com/products/kubernetes/details/managed/
8. https://keda.sh/docs/latest/deploy/

## Design decisions

Same form as the design record: the choice, what was not taken, why, and the cost.

### S1. DOKS with a Spot GPU node pool

- **Choice.** One DOKS cluster: a small CPU pool, and a `gpu-mi350x1-288gb-spot` pool created
  with autoscaling from 1 to 2 nodes.
- **Not taken.** An on-demand GPU pool in DOKS. k3s on Spot GPU Droplets made with
  `provision/01_create_spot.sh`.
- **Why.** The plan asks for GPU Kubernetes with autoscaling, on the same spot GPUs as the
  drill. DOKS lists that exact size as a Spot node pool, so the managed path exists and
  node autoscaling comes with it. On-demand MI350X is contract only, and the on-demand AMD
  size that can be bought (MI300X) is a different GPU from the drill's. k3s on Droplets
  would reuse the proven provisioning scripts, but node autoscaling would have to be
  written by hand, which is the thing being tested.
- **Cost.** Spot node pools are a public preview. The pool's maximum is fixed at creation.
  Reclamation takes the whole pool, so two replicas in one Spot pool do not protect against
  it. Surviving a real reclamation needs an on-demand pool behind a priority expander,
  which is not built here.
- **If the Spot pool cannot be created.** Fall back in this order. (1) Another Spot size or
  region the preflight shows, by changing `SERVING_GPU_SIZE` and `SERVING_REGION`. (2) An
  on-demand `gpu-mi300x1-192gb` pool: same manifests, a different GPU, so the numbers are
  not MI350X numbers and must be labelled. (3) k3s on Spot GPU Droplets: install the AMD
  device plugin by hand and apply the same manifests; node autoscaling is then out of
  scope and only pod-level results can be recorded.

### S2. KEDA on vLLM's in-flight request count

- **Choice.** A KEDA `ScaledObject` with a Prometheus trigger on
  `sum(vllm:num_requests_waiting) + sum(vllm:num_requests_running)`, as an average per
  replica.
- **Not taken.** HPA with prometheus-adapter. Scaling on GPU utilisation. Scaling on
  waiting requests alone.
- **Why.** Both KEDA and the adapter need a Prometheus. KEDA puts the query in one object
  next to the workload; the adapter needs a rule file and an APIService that maps series to
  Kubernetes metrics, which is more to get wrong in a short paid session. GPU utilisation
  sits near 100% whether one request is running or fifty. Waiting alone is the honest queue
  depth, but it drops to zero when the new replica absorbs the queue, and the autoscaler
  would then remove the replica that fixed it. Running plus waiting stays up while the load
  is there.
- **Cost.** One more operator and its CRDs. The target per replica is a number that has to
  come from a measurement, so it ships as a placeholder. Scale-up is slow by nature: a new
  replica needs a new node, an image pull, a model download and a model load.

### S3. Drain in two steps

- **Choice.** On termination a pod first sleeps 15 s in `preStop` while still serving, then
  vLLM gets SIGTERM with `--shutdown-timeout=120`, inside a 180 s grace period. A
  PodDisruptionBudget allows one pod to be disrupted at a time.
- **Not taken.** Relying on SIGTERM alone. `minAvailable: 1`.
- **Why.** This is the serving analogue of the drill's drain path. Kubernetes removes a
  terminating pod from the Service at once, but that takes a moment to reach every node.
  The sleep covers it, so new requests stop arriving before the server is told to stop.
  Then the server finishes what it has. `minAvailable: 1` with one replica would block
  every drain until the node is taken anyway.
- **Cost.** Every termination takes at least 15 s. A request longer than the shutdown
  timeout is cut. Whether vLLM really finishes open streams within that timeout is not
  verified (see the table above).

### S4. A node-local model cache

- **Choice.** `hostPath` on the GPU node for the Hugging Face cache.
- **Not taken.** A block storage volume per replica. Baking weights into an image.
- **Why.** DigitalOcean block volumes attach to one node, so replicas cannot share one. A
  volume per replica is one more billed thing to forget at teardown. The node-local cache
  survives pod restarts on a node and dies with the node, which is what a spot node does.
- **Cost.** Every new node downloads the weights again. That time is inside the
  autoscaling result, where it belongs. `hostPath` is acceptable only because the cluster
  is single-tenant and short-lived.

### S5. No public endpoint

- **Choice.** `ClusterIP` only. The load generator runs in a pod on the CPU pool.
- **Not taken.** A load balancer. `kubectl port-forward` from the laptop.
- **Why.** vLLM is started without an API key. A load balancer is a billed resource that
  outlives a failed teardown. A port-forward pins to one pod, so a second replica would
  never see load, and the laptop's network would be in every latency number.
- **Cost.** Latency is measured from inside the cluster. It does not include the public
  internet or TLS.

### S6. Fake results cannot become numbers

- **Choice.** The fake server labels every response. `loadgen.py` writes
  `is_fake_server: true` and `publishable: false` when it sees the label.
  `cost_per_mtok.py` refuses such a file unless told otherwise, and then prefixes every
  line with `FAKE INPUT, NOT SERVING NUMBERS`.
- **Why.** N6 in the design record: simulated parts are labelled. The fake server is what
  makes the tools testable without a GPU, and it is also the easiest way to produce a
  plausible-looking table by accident.
