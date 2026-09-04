# Security notes

## Secrets

Keep these values only in `.env`:

- `DO_TOKEN`
- `SPACES_KEY`
- `SPACES_SECRET`
- `DRILL_TOKEN`
- `REDIS_PASSWORD`
- `CONTROLLER_IP`

`.env` and `.state/` are ignored by Git. Check `git status` before every commit.

Use a Spaces key scoped to one private checkpoint bucket. Rotate it after the drill.

`provision/02_bootstrap.sh` deliberately excludes `DO_TOKEN` when it sends configuration to GPU Droplets.

## Network exposure

The controller binds Redis and the webhook to public interfaces because Spot GPU Droplet IPs change. `provision/00_controller.sh` creates tag-scoped firewalls and limits operator access to the public IP detected at creation time.

If the operator's public IP changes, update the firewall before continuing.

The controller's `GET /events` and `GET /status` routes are not token-protected. Their protection depends on the cloud firewall. Do not run the controller without that firewall.

The bootstrap script stops the image's stock Jupyter container because this drill does not need its public ports.

## Scratch-device safety

`provision/02_bootstrap.sh` may format `SCRATCH_DEV`. Verify the device manually with `lsblk` and `findmnt` before setting it. Never guess the device path.

## Logs

Raw logs can contain:

- public and private IP addresses
- Droplet IDs and names
- bucket names
- local usernames and paths
- pricing and account metadata

Review and redact logs before publishing them.

## Reporting a vulnerability

Open a GitHub issue without including credentials, tokens, private IPs, or account identifiers. If disclosure through a public issue would create risk, contact the repository owner privately through their GitHub profile.
