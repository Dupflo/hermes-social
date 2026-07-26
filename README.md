# Hermes Social

[Hermes Agent](https://hermes-agent.nousresearch.com) packaged with a social
media toolset — one repo, one buildable image, one command to stand up a new
VPS.

## Layout

| Path | What | Touch it? |
| --- | --- | --- |
| `core/` | Vendored Hermes Agent source (MIT, Nous Research) | **Never modify** — replaced wholesale on upstream updates |
| `social/skills/` | Social media skills, loaded via `skills.external_dirs` | Yes — this is where the work happens |
| `social/tools/` | Standalone services (webhooks, schedulers) | Yes |
| `social/config/` | Config snippets the social layer needs | Yes |
| `deploy/` | Compose stack (hermes + camofox), env template, bootstrap | Yes |
| `Dockerfile` | Overlay: core image + `social/` payload | Rarely |

## Quick start (fresh VPS)

```bash
git clone https://github.com/Dupflo/hermes-social
cd hermes-social/deploy
./bootstrap.sh        # creates .env from template on first run
vim .env              # fill in secrets
./bootstrap.sh        # builds images, starts the stack
```

Then merge `social/config/config.social.yaml` into `deploy/data/config.yaml`
(created on first agent run) and restart.

## Build

```bash
make            # core image + overlay  → hermes-social:latest
make camofox    # browser image (clones jo-inc/camofox-browser as a sibling)
```

## Security model

- **The repo is public — no secrets, ever.** Secrets live only in
  `deploy/.env` (gitignored, `chmod 600`). Skills read them from the
  environment.
- Nothing is published on public interfaces by default: the Hermes UI (4860),
  noVNC (6080) and VNC (5901) bind to `127.0.0.1`. Reach them over an SSH
  tunnel (`ssh -L 4860:127.0.0.1:4860 root@<vps>`) or front them with a
  reverse proxy + auth.
- The compose file mounts `/var/run/docker.sock` into the agent — deliberate:
  the agent manages containers on its own VPS. That is root-equivalent on the
  host; remove the mount on any VPS where that trust isn't wanted.

## Updating the vendored core

```bash
# Get the new upstream source, then:
rsync -a --delete --exclude='.git' <new-hermes-source>/ core/
git add core && git commit -m "core: bump to upstream <version>"
```

Local additions never live in `core/`, so the update is always conflict-free.
If a core patch ever becomes unavoidable, add it as a `patches/*.diff` applied
in the Dockerfile — never edit `core/` in place.

## License

MIT. `core/` is © Nous Research; the social layer is © Florian Dupuis.
