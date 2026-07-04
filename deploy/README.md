# Deploying james

james is one long-running process — `james serve` — that polls Telegram /
connects to Discord and dispatches to backends. Deployment is the same shape
everywhere:

1. **Runtime** — provide the flox env (host install) or a container image.
2. **Config** — `config.yaml` (structure + the fail-closed allowlist).
3. **Secrets** — bot tokens / API keys in the **environment**, from a SOPS+age
   dotenv (`sops exec-env`) or your orchestrator's secret store. Never in git
   or the image.
4. **Backends** — see the critical distinction below.

## cli backends vs api backends (read this first)

This decides *where* you can run james:

- **`api` backends** (`gpt`, any OpenAI-compatible endpoint) need only an env
  var (`OPENAI_API_KEY`). They run **anywhere** — container or host.
- **`a2a` backends** (`openclaw`) need only a bearer token env var plus network
  reach to the peer's A2A endpoint. They too run **anywhere**.
- **`cli` backends** (`claude`, `codex`, `grok`, `opencode`) shell out to the
  vendor CLI using its **interactive login**, whose state lives in the user's
  home (`~/.claude`, `~/.codex`, `~/.grok`, opencode's data dir). They need:
  the vendor CLI installed, a one-time login done **as the service user**, and
  that home persisted.
  (Exception: `shot` is a cli backend but needs no login — headless Chromium,
  already in the flox env — so it runs anywhere, container included.)

So: **host install** (systemd / Ansible / NixOS module) is the natural home for
cli backends — the host has the CLIs logged in. **Containers** (Docker / Nomad)
are cleanest for api backends; to run cli backends in a container you must bake
the vendor CLIs into the image and mount their login-state dirs as volumes.

---

## flox (the base everywhere)

`flox activate` provides the toolchain; `uv sync` installs deps. For a quick
managed run there's a service entry in the manifest:

```bash
sops exec-env secrets.enc.env 'flox activate --start-services'   # runs `james serve`
flox services status james
flox services logs james
```

For production, put it under systemd / Nomad below (more robust than flox
services).

## Host install — systemd (supports all backends)

1. Check out the repo to `/opt/james`, create a `james` user **whose home is
   `/opt/james`** (the unit pins `HOME=/opt/james`, so logins must land there).
2. As that user, **with that HOME**: `sudo -u james -H bash -lc 'cd /opt/james &&
   flox activate -- uv sync --frozen --no-dev'`, then the one-time logins you'll
   use, e.g. `sudo -u james -H bash -lc 'cd /opt/james && flox activate -- claude'`
   (and `codex` / `grok`). The `-H` is what makes `~/.claude` land where the
   service later looks.
3. Drop your SOPS-encrypted dotenv at `/opt/james/secrets.enc.env` (and the age
   key for the service user). Tip: for a tighter blast radius keep these (and set
   `working_dir`) *outside* the checkout — the agent has read/write there.
4. Install [`systemd/james.service`](systemd/james.service), then
   `systemctl enable --now james`.

## Host install — Ansible (supports all backends)

[`ansible/`](ansible/) automates the above across hosts: it checks out the repo,
runs `flox … uv sync`, renders `config.yaml`, ships the encrypted secrets, and
manages the systemd unit.

```bash
cd deploy/ansible
cp inventory.example.ini inventory.ini      # edit hosts + vars
# place your encrypted dotenv at files/secrets.enc.env (stays encrypted at rest)
ansible-playbook -i inventory.ini playbook.yml
```

Targets need `flox`, `sops`, and an age key (add a preceding role if your base
image lacks them). Override any default in `roles/james/defaults/main.yml` via
inventory / group_vars. cli-backend logins are still a one-time manual step as
the `james` user.

## Nix / NixOS module (supports all backends)

[`../flake.nix`](../flake.nix) exposes:

- `devShells.default` — the same toolchain as flox (`nix develop`; deps still via
  `uv`).
- `nixosModules.james` — a systemd service that runs `flox activate -- ./james
  serve` (flox provides the app runtime; Nix provides system integration).

```nix
# flake.nix (your host config)
{
  inputs.james.url = "github:jim-technologies/james";
  outputs = { nixpkgs, james, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        james.nixosModules.james
        {
          services.james = {
            enable = true;
            workingDir = "/opt/james";           # checked-out repo
            environmentFile = "/run/secrets/james.env";  # decrypted dotenv
          };
        }
      ];
    };
  };
}
```

(The Python deps stay in `uv.lock` rather than being re-packaged in Nix —
matching the flox stance. `flox` must be available on the host, e.g. via
`environment.systemPackages = [ pkgs.flox ]`.)

## Container — Docker / Nomad (api backends; cli with extra work)

Build a self-contained image and run it on Nomad.

```bash
docker build -f deploy/Dockerfile -t REGISTRY/james:0.1.0 .
docker push REGISTRY/james:0.1.0
nomad job run deploy/nomad/james.nomad.hcl     # edit REGISTRY / datacenter / Vault path
```

The image bundles the source + deps and runs `python -m app serve`. The
[Nomad job](nomad/james.nomad.hcl) renders `config.yaml` and the secrets env from
your secret store (Vault template by default) and binds the config over the
image's default. See the note at the bottom of that file for running cli backends
in a container.

> **flox-native alternative.** `flox containerize` produces a toolchain image but
> **does not bundle your source** (verified: it carries only the env). To use it,
> mount the repo at the `working-dir` set in `[containerize.config]` (manifest)
> and the baked `cmd` runs `uv sync --frozen --no-dev && ./james serve`. The
> `deploy/Dockerfile` above is the simpler, self-contained option.

## Session memory (per-thread context)

Each chat thread (a Telegram topic / Discord thread) maps to a persistent agent
session, so follow-ups resume context and you can answer the agent's questions
in-thread. `/reset` forgets a thread. This depends on two pieces of state staying
durable across restarts/redeploys:

- **The james session store** (`sessions.store_path`). The dev default
  (`.james-sessions.json`) lands **inside the checkout** — a redeploy that
  re-clones or rebuilds the image wipes it, silently resetting all thread memory.
  Point it at durable storage: the systemd unit sets `StateDirectory=james`
  (→ `/var/lib/james`), so use `/var/lib/james/sessions.json` (the Ansible role
  renders this by default). On Nomad use `/alloc/data/sessions.json` or, to
  survive a reschedule, a host volume.
- **Each CLI's own session state**, alongside the store. claude keeps sessions
  under `~/.claude` (keyed by working dir); codex under `~/.codex`, grok under
  `~/.grok`, opencode under its config/data dir. A resume only works if that
  state and `HOME`/`working_dir` are **stable** across redeploys — keep them on
  the **same** persistence boundary as `sessions.store_path` so the two never
  diverge. If they do (HOME wiped, checkout moved, transcript pruned), james
  detects the CLI's dead-session message and **self-heals by starting a fresh
  session** for that thread (prior context lost, thread keeps working — no manual
  `/reset`).

Capture-backend gotchas (codex / grok / opencode):

- **Resume is always by the exact captured id**, never "continue last" — james
  deliberately does not use `grok -c`/bare `-r` or `opencode -c`, which are
  *cwd-scoped* and would cross-contaminate threads that share a working dir. Keep
  `working_dir` stable for the same reason (these CLIs key their session lists on
  cwd; moving it orphans sessions, triggering the self-heal above).
- **Machine-readable output is mandatory**: capture parses each CLI's JSON
  (`--json` / `--output-format json` / `--format json`, set in the backend rows).
  Don't disable it or memory silently stops working.
- **opencode → z.ai**: log in once with `opencode auth login` (pick z.ai/GLM) as
  the service user, into the same `$HOME` the unit runs as; the backend uses
  `zai-coding-plan/glm-4.7`. Verify the live resume round-trip once after
  wiring z.ai: `tests/live/test_live.py::test_live_opencode_session_roundtrip`
  (`RUN_LIVE_TESTS=1`).

## Browser profiles (the `shot` backend)

Each profile is a persistent, **logged-in** Chrome user-data-dir under
`browser.profiles_dir` — treat the whole directory as a credential.

- **Persistent + private storage.** Put `profiles_dir` on storage that survives
  restarts and is owned 0700 by the service user: the systemd unit sets
  `StateDirectory=james` (→ `/var/lib/james`), so point `browser.profiles_dir` at
  `/var/lib/james/profiles`. On Nomad, use a host volume mounted there. Never
  commit it (it's gitignored).
- **Seeding logins is interactive and needs a display.** Run `james login <name>`
  where there's a GUI — your workstation, or the host over VNC/X-forwarding —
  sign in, close the window. The seeded profile dir is then reused headlessly.
  For container/Nomad deploys (no display), seed on a workstation and copy the
  profile dir onto the host volume, or only use the default (logged-out) profile.
- **Concurrency.** Same-profile runs serialize (Chrome locks a profile); size
  `max_concurrency` for memory — each concurrent `shot` is a Chromium process
  (~1GB headroom recommended; see the Nomad job's resources note).

## Web dashboard (optional)

Off by default. Enable in `config.yaml` (`web.enabled: true`) and set the
Basic-auth password in the sops dotenv under the env var named by `web.token_env`
(default `JAMES_WEB_PASSWORD`) — an unset/empty password serves nobody
(fail-closed). It binds `web.bind_host` (default `127.0.0.1`) on `web.port`.

- **Never expose it bare.** Keep `bind_host: 127.0.0.1` and reach it via an SSH
  tunnel, or front it with a **TLS-terminating authenticating reverse proxy**
  (caddy/nginx) — the loopback bind + Basic auth is the floor, not transport
  security. Anyone who reaches it can drive your agents.
- **No new secret surface.** A web prompt runs through the same `dispatch` path
  as the channels, so the agent secret-stripping applies; the web layer never
  reads transcripts or agent state.
- **systemd/Ansible:** the unit already serves it in-process — just open the port
  to your proxy/tunnel, not the world. **Nomad/Docker:** publish `web.port` only
  to the proxy network; set the password via the Vault/`nomadVar` template like
  the bot tokens.

## Telegram: finding your chat id

Go-live is fail-closed on `allowed_chat_ids`, so you must add your id or the bot
silently ignores you. To find it: message **@userinfobot**, or after setting the
token run:

```bash
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" \
  | grep -o '"chat":{"id":[0-9-]*' | head   # message the bot first
```

Put the number in `config.yaml` `allowed_chat_ids: [123456789]` and restart.
(Same idea for Discord `allowed_channel_ids` — enable Developer Mode, right-click
the channel → Copy ID.)

## Sanity check after deploy

Run these **as the service user with the same HOME** the unit uses, so they
exercise the real login state and non-interactive activation:

```bash
# (host install) run exactly as the service will:
sudo -u james -H sops exec-env /opt/james/secrets.enc.env \
  'flox activate -- ./james cli --backend claude "say hi"'   # cli login + activation
./james cli --backend gpt "say hi"          # api backend (needs OPENAI_API_KEY)
./james cli --backend shot https://example.com   # headless Chromium -> PNG
```

A non-allowlisted chat gets no reply (fail-closed). Logs go to the supervisor
(`journalctl -u james`, `nomad alloc logs`, `flox services logs james`).
