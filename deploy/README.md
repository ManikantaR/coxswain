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
ssh nas 'docker exec -it -u coxw -e HOME=/home/coxw coxd claude login'     # device-code flow → auth in a browser.
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

## The board's Deploy button (via the deployer)
coxd does **not** run deploys itself: it has no `docker.sock` and no `/volume1/docker`.
After the CI gate, `run_deploy` calls the
[deployer](#deployer-optional-opt-in) (`POST /deploy/<repo>/<sha>`, then streams the job log).
The deployer runs the allowlisted argv for that repo name; the registry's `deploy.command`
only marks the repo as deployable, coxd never executes it. The board button and the
post-merge hook (subject to the holds below) are the only callers of the deployer.

Setup: run the deployer (below) and set `COXD_DEPLOYER_TOKEN` (same value as the
deployer's `DEPLOYER_TOKEN`) in `deploy/.env`; set `COXD_DEPLOYER_URL=http://deployer:8792`
to enable it. With the URL unset the board's Deploy returns an error and calls nothing.

**Source freshness is verified.** coxd calls `POST /deploy/<repo>/<sha>` with the merged
commit (40 hex characters, else 400; the body is never read). The deployer exports
`EXPECTED_SHA=<sha>` to the allowlisted command, which must fetch and check out that commit.
After the command exits 0 the deployer runs `git -C <cwd> rev-parse HEAD`; if it differs (or
cannot be read) the job is `failed` and coxd records `deploy-failed` and pings. The reason is
`stale-source` when HEAD is a different commit, and `head-unreadable` when git could not read HEAD
at all (the check runs with `safe.directory` scoped to that one cwd, so a checkout owned by
another uid is fine). The deployer image includes `git`.

Networks: the deployer sits on `deployer-internal` (`internal: true`, the only network coxd
shares with it) and also on `deployer-egress`, a normal network, so its command can reach
GitHub to fetch. It publishes no ports and is not on `traefik-public`; nothing outside
`deployer-internal` can reach it.

Post-merge deploys: a merged repo with `deploy.enabled` deploys its merge commit. It is **held**
(`deploy-held` event + ping, nothing deployed) when any commit between the last successfully
deployed one and the merge commit touches migration files (`deploy.migration_dirs`, default `migrations`, `alembic`;
multi-segment values like `db/migrations` match; #53) - so a held migration stays held until a human deploys it - when
the merge commit is not strictly ahead of the last deployed one (no rollbacks from a
late-detected older merge), when there is no deployed baseline yet (deploy once by hand; a
successful deploy of any kind records it), when the comparison fails, when the merge commit
cannot be resolved, when the PR did not merge into `deploy.branch` (default `main`), or when another deploy for this repo is running. The manual board button deploys the current head of
`deploy.branch` (default `main`). A 409 (deploy already running) ends the attempt as
`deploy-start` then `deploy-skipped` and does not write the coalesce stamp; repeat deploys are
only coalesced for the *same* commit within 3 minutes.

Rules for a repo's deploy command (the allowlist entry):
- **Sync to `$EXPECTED_SHA` yourself.** coxd's clone in `/repo` is not visible to the deployer,
  so the command must `git fetch` and `git checkout --detach "$EXPECTED_SHA"` in its cwd
  before building (the deployer only verifies HEAD afterwards).
- **Container-native — no `ssh nas`.** Use a script like MoneyPulse's `deploy/coxd-deploy.sh`.
- Non-interactive (stdin is `/dev/null`). coxd stops waiting after `deploy.timeout_s`
  (default 2700 s = 45 min) and records `deploy-failed` rc=124. The deployer also kills a job
  after `DEPLOYER_JOB_TIMEOUT_S` (default 2700), so a hung build cannot block later deploys;
  coxd passes its `deploy.timeout_s` as `?timeout_s=` and the deployer uses
  `min(that, DEPLOYER_JOB_TIMEOUT_S)`, so the two cannot disagree beyond that cap.

Watching a deploy: the deployer log is relayed live to `docker logs -f coxd` (lines prefixed
`[deploy <task-id>]`), and a `deploy-progress` event with the latest line lands in the
task's Events feed every ~20 s. Every deploy ends in exactly one of `deployed` /
`deploy-failed` (including timeouts and coxd-side crashes) — a `deploy-start` with nothing
after it for >45 min means coxd itself died; check `docker logs coxd`.

History: before this, the deploy ran via `subprocess.run(capture_output=True)` with no
timeout and Python stdout was block-buffered — so a multi-minute build looked dead, a hung
step blocked forever, and `docker logs` was empty ("Deploy dies silently", 2026-08-22).

## Upgrading to the pinned / persistent image (factory Phase 0, 2026-10-31)

The image now sets `CLAUDE_CONFIG_DIR=/root/.claude` so the CLI's account state lives in
the `claude-creds` volume (it used to sit at `/root/.claude.json`, outside every volume,
so each rebuild logged coxd out). git→GitHub auth is baked into `/etc/gitconfig` (gh
credential helper), so `gh auth setup-git` is no longer needed after a rebuild.

One-time, **before** the first rebuild with this image, carry the existing login into
the volume so you don't have to log in again:

```bash
ssh nas docker exec coxd cp -p /root/.claude.json /root/.claude/.claude.json
```

After the rebuild, verify both survive:

```bash
ssh nas 'docker exec coxd sh -c "cd /tmp && claude -p \"reply ok\" && git -C /repo/MoneyPulse ls-remote origin HEAD"'
```

Versions are pinned (`CLAUDE_CODE_VERSION`, `CLAUDE_AGENT_SDK_VERSION` build args) and
the CLI's self-updater is off. Bump deliberately, then re-run a proving task.


## Worker sandbox (2026-10-31)

The coxd process runs as **root** (store, registry, GitHub/Telegram credentials, push).
Everything the LLM can influence runs as the unprivileged **`coxw`** user (uid 10001) via
`setpriv`: the claude CLI and its tools (`deploy/coxd-claude`, `ClaudeAgentOptions.cli_path`),
the gate's test/lint/build, `pnpm install`, and any git command inside a task worktree.
See `coxd/sandbox.py` and `deploy/entrypoint.sh`.

- `coxw` owns `/work` (task worktrees) and `/home/coxw/.claude` (the claude login).
- `/repo/*/.git` objects and refs are group-writable; `.git`, `.git/config` and `.git/hooks`
  stay root-only, so a worker cannot make root run its code.
- Root never runs git inside a worktree: push and `gh pr create` run from `/repo/<name>`.
- No `docker.sock` and no `/volume1/docker` in the container; deploys go through the
  separate deployer container (see the Deploy button section).
- `claude login` must now run as coxw (see the setup step above).
- After every coxd deploy, prove the boundary:
  `ssh nas 'docker exec -i coxd sh -s' < deploy/smoke-sandbox.sh` must print `ALL OK`.
  Its mount check is fail-closed: only the compose mountpoints (`/`, `/data/coxswain`, `/repo`,
  `/work`, `/home/coxw/.claude`, `/etc/{hosts,hostname,resolv.conf}`, `/proc`, `/sys`, `/dev`) and
  local filesystem types are allowed, and every disk/network-backed mount (btrfs, ext*, xfs, nfs*,
  cifs, smb*, fuse*) must be a docker named volume (`@docker/volumes/<name>/_data`) or a container
  `resolv.conf`/`hostname`/`hosts` file. A NAS share bound anywhere, even onto `/repo`, prints FAIL.
  A repo with `data_globs` may get `intake: true` only after this passes.

## Deployer (optional, opt-in)

A separate `deployer` container is the only thing with `docker.sock`; coxd reaches it over the
internal `deployer-internal` network. It is behind a compose profile, so plain
`docker compose up -d` (what `deploy-coxd.sh` runs) never starts it and needs none of these files.

1. Create `deploy/deployer.env` on the NAS (never commit it):
   ```
   DEPLOYER_TOKEN=<long random string>
   ```
2. Create `deploy/deployer/allowlist.json` on the NAS (never commit it). Repo name -> argv + cwd.
   `cwd` must be inside `DEPLOYER_SRC_ROOT` (default `/volume1/docker`, mounted at the same path
   inside the container). Invented example:
   ```json
   {
     "example-app": {
       "cmd": ["sh", "-c", "git fetch origin && git checkout --detach \"$EXPECTED_SHA\" && docker compose up -d --build"],
       "cwd": "/volume1/docker/example-app"
     }
   }
   ```
   The command must sync to `$EXPECTED_SHA` before building; the deployer only verifies HEAD
   afterwards (a stale tree fails the job). If the checkout is owned by another uid, add
   `-c safe.directory=<cwd>` to the git calls in the command as well.
3. Start it: `docker compose -f docker-compose.coxd.yml --profile deployer up -d --build deployer`.

If the allowlist is missing, a directory, or has a cwd outside the root, the deployer logs a
`FATAL config error` line and exits (retried at most 3 times).

API: `POST /deploy/<repo>/<sha>[?timeout_s=N]`, `GET /deploys/<id>/status`, `GET /deploys/<id>/log`, bearer token.
Only the last 50 deploys are kept (older logs are deleted); history is in memory and is lost on
restart.

## Owner login (board writes)

Without this the board has no login: anything on the LAN can press Approve auto-merge,
Deploy, Resume, etc. With `COXD_OWNER_USER` set, every state-changing request (POST) must
come through the forward-auth proxy as the owner, or carry the local admin token.
All values below are invented examples.

1. Close Traefik's open API (`api.insecure`) first; it exposes middleware header settings.
2. `docker network create coxd-proxy`, and attach the Traefik container to it. Then deploy
   with the override `docker-compose.owner-login.yml` (`deploy-coxd.sh` picks it automatically when the NAS `deploy/.env` has a non-empty
   `COXD_AUTH_MIDDLEWARE` (step 4) and prints which mode it deployed; by hand, add
   `-f docker-compose.owner-login.yml`; needs Compose >= 2.24). The override also carries
   the router's middleware label, so the base file's labels stay unchanged.
   It moves coxd off `traefik-public` onto `coxd-proxy`, so other containers can't reach it
   directly. Without the override the base file is unchanged and needs no extra network
   (then the proxy secret is the only barrier against containers on `traefik-public`, and
   they can still read GET pages).
3. Define the forward-auth middleware in Traefik (Authelia), adding the shared secret:
   ```yaml
   http:
     middlewares:
       authelia:
         forwardAuth:
           address: http://authelia:9091/api/authz/forward-auth
           authResponseHeaders: [Remote-User]
       coxd-secret:
         headers:
           customRequestHeaders:
             X-Coxd-Proxy-Secret: "s3cret-example-0000"
       coxd-chain:
         chain:
           middlewares: [authelia, coxd-secret]
   ```
   Authelia must also strip any client-sent `Remote-User` (it overwrites it on success).
4. In `deploy/.env`: `COXD_AUTH_MIDDLEWARE=coxd-chain@file`, `COXD_OWNER_USER=alice`,
   `COXD_PROXY_SECRET=s3cret-example-0000` (same value as step 3). The middleware covers
   the whole board host, GET pages included. Writes need the right `Remote-User` AND the
   secret; either alone gets 403. Leave `COXD_OWNER_USER` unset to turn enforcement off.
5. Local operator calls (no loopback exemption — workers share localhost): coxd creates
   `$COXD_HOME/admin.token` (when `COXD_OWNER_USER` is set) (mode 600, root) on first start. Send it as a header:
   `docker exec coxd sh -c 'curl -s -X POST -H "X-Coxd-Admin-Token: $(cat /data/coxswain/admin.token)" localhost:8791/api/factory -d "{\"paused\":true}"'`

`COXD_PROXY_SECRET` is in `envscrub.SECRET_VARS`, so workers never inherit it.

## PII denylist health

The denylist (`$COXD_HOME/pii-denylist.txt`) only protects what it contains, so coxd tracks its
freshness. The owner's review tool writes two metadata files into `$COXD_HOME/pii/`:

- `last-reviewed` — epoch seconds of the last review
- `new-candidates.count` — integer count of unreviewed candidate terms
- `harvest-failed.count` — integer count of sources the review tool failed to read (>0 → at least `due`)

Coxd reads those metadata files and parses the denylist in-process only to count effective terms
(`coxd/pii_health.py`), so a missing, empty or comment-only file is `blocked`. It returns only
booleans and counts and never prints or logs terms.

| level | condition | effect |
|---|---|---|
| `ok` | reviewed ≤30 days ago, 0 new candidates | none |
| `due` | >30 days, or new candidates >0 | digest line, pings, amber board pill |
| `overdue` | >45 days (or never reviewed) | nags only; does not block work |
| `blocked` | denylist missing or empty | fail closed (below) |

Reminders (counts only): one line in the morning digest when not `ok`; a Telegram/ntfy ping (sent only at or after `COXD_DIGEST_AT`) on the
1st of each month, then every 3 days while `due`/`overdue`, stopping once a review lands; a
`denylist 12d` pill in the board header (warning colours when due/overdue/blocked).

Fail closed when `blocked`: `intake.poll_once` takes nothing new (one log line per poll cycle), and
`ship` blocks the PR with `pii-scan-error` for any repo with `intake: true` or `data_globs`. Other
repos keep the old behaviour (a `pii-denylist-missing` event).

## Model and effort per role

| Var | Default | Meaning |
|---|---|---|
| `COXD_WORKER_MODEL` | `claude-sonnet-5-5` | worker model |
| `COXD_WORKER_EFFORT` | `high` | worker effort: `low\|medium\|high\|xhigh\|max` |
| `COXD_REVIEW_MODEL` | `claude-opus-5-5` | reviewer model |
| `COXD_REVIEW_EFFORT` | `medium` | reviewer effort |

Set them in `deploy/.env`; empty/unset keeps the default, and an invalid effort is ignored.
A repo's registry entry may set `worker_model`, `worker_effort`, `review_model`,
`review_effort`; precedence is **registry > env > default**. Each run records the
effective values in a `config` event.
