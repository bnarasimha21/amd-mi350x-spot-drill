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
