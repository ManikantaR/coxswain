# Running coxd 24/7 on the NAS

The NAS host has only Docker (no git/gh/node/claude), so coxd ships as one container
(`deploy/Dockerfile`) with three persistent volumes: the sqlite store + registry
(`coxd-data`), the Claude creds (`claude-creds`), and the target repos (`coxd-repos`).
It rides the **Claude Pro subscription** via a one-time `claude login` — no API key.

## One-time setup

**1. Secrets** — create `deploy/.env` (gitignored):
```
GH_TOKEN=ghp_xxx            # a PAT (repo scope) or fine-grained token for the target repos
COXD_NTFY_TOPIC=coxd-<you>  # optional AFK pings via ntfy
```

**2. Ship + build on the NAS** (from your Mac):
```bash
./deploy/deploy-coxd.sh          # rsyncs coxswain to the NAS and `docker compose up -d --build`
```

**3. 👉 YOUR STEP — authenticate (I can't; it needs your login):**
```bash
ssh nas 'docker exec -it coxd claude login'     # device-code flow → auth in a browser.
                                                # Creds persist in the claude-creds volume.
ssh nas 'docker exec coxd gh auth setup-git'    # make git push/clone use GH_TOKEN
```
Claude on Linux writes `~/.claude/.credentials.json` (Mac uses Keychain — not portable —
which is why this login happens *inside* the container). It refreshes itself from then on.

**4. Clone the target repos into the repo volume:**
```bash
ssh nas 'docker exec coxd sh -lc "cd /repo && \
  gh repo clone ManikantaR/MyMoney && gh repo clone ManikantaR/aura-tutor"'
```

**5. Verify:**
```bash
ssh nas 'docker exec coxd python /app/coxd/cli.py list'   # empty list = healthy
# board: http://<nas>:8791/   (put behind Traefik + auth before exposing for the PWA)
```

## Day-to-day
- Dispatch/status/tail from any lead session over the CLI (`docker exec coxd python /app/coxd/cli.py …`)
  or the board API on `:8791`. The `/coxd` skill knows the flow.
- Update coxd: re-run `./deploy/deploy-coxd.sh` (rebuilds; the volumes persist store + creds + repos).
- Per-repo config (deploy command, sacred_globs) lives in the `coxd-data` volume at
  `/data/coxswain/repos/<name>.json` — edit with `docker exec`.

## The known risk
Headless refresh of the Pro-subscription OAuth token is the one unproven bit. If `claude login`
inside the container won't hold, the fallback is `ANTHROPIC_API_KEY` in `.env` (API billing, not
Pro) — coxd's SDK honors it. Try the Pro login first.

## The board's Deploy button (in-container deploys)
Deploy runs the repo's registry `deploy.command` **inside the coxd container**, with cwd =
the repo clone in `/repo/<name>`, after `git pull --ff-only origin <branch>` and a CI gate.
The container drives the host Docker through the mounted `/var/run/docker.sock` and sees
`/volume1/docker` at the same path the app compose files use.

Rules for a repo's deploy command on the NAS:
- **Must be container-native — no `ssh nas`.** The image has no ssh client or key and the
  NAS can't ssh to itself from here. Use a script like MoneyPulse's
  `deploy/coxd-deploy.sh` (sync build context into `/volume1/docker/<app>/repo` →
  `docker compose build` → `up -d --force-recreate` → migrate). A workstation script like
  aura-tutor's `./deploy-to-nas.sh` (ssh/scp) will fail here — aura-tutor needs its own
  `coxd-deploy.sh` before its Deploy button can work on the NAS.
- Non-interactive (stdin is `/dev/null`) and bounded: coxd kills it after
  `deploy.timeout_s` (default 2700 s = 45 min) and records `deploy-failed` rc=124.
- `docker compose build` needs the buildx plugin (compose v5 builds via `buildx bake`);
  it's in the image.

Watching a deploy: output streams live to `docker logs -f coxd` (lines prefixed
`[deploy <task-id>]`), and a `deploy-progress` event with the latest line lands in the
task's Events feed every ~20 s. Every deploy ends in exactly one of `deployed` /
`deploy-failed` (including timeouts and coxd-side crashes) — a `deploy-start` with nothing
after it for >45 min means coxd itself died; check `docker logs coxd`.

History: before this, the deploy ran via `subprocess.run(capture_output=True)` with no
timeout and Python stdout was block-buffered — so a multi-minute build looked dead, a hung
step blocked forever, and `docker logs` was empty ("Deploy dies silently", 2026-08-22).
