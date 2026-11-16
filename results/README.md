# Results

New runs write local timing, loss, event, and receipt files under this directory.

Raw artifacts from the 2026-08-18 reference run are intentionally excluded because they contain infrastructure identifiers. The publishable measurements are:

| Measurement | Result |
| --- | ---: |
| Model | Qwen3-8B full fine-tune |
| GPU | AMD Instinct™ MI350X |
| Checkpoint | 98,289,353,505 bytes |
| Checkpoint write | 56.5 s |
| Upload | 264 s at 2.97 Gbps |
| Notice to durable state | 322.6 s |
| Replacement to usable GPU | 201 s |
| Bootstrap | 96 s |
| Download | 244 s |
| Load and resume | about 40 s |
| Resume step | 14,625 |
| Steps redone | 0 |

These results are from one run. Do not treat them as an SLA or expected platform performance.

Before publishing output from a new run, remove credentials, IP addresses, Droplet IDs, bucket names, hostnames, usernames, and local paths.

## Serving

No serving measurements exist yet. The serving half has been built and tested without a GPU and has not been run on one. When the session in `SERVING.md` is run, its raw files go under `results/serving/` (ignored by Git) and the publishable numbers go into the results table in `SERVING.md`. Files produced against `serving/fake_openai_server.py` are marked `is_fake_server: true` and are never serving numbers.
