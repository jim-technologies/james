# james

A personal AI chief-of-staff: an open-source **harness** for agentic backends.
You message it a task from **Telegram** or **Discord**; james routes it to an AI
agent (claude / codex / grok / opencode, or an API model), gives that agent your
tools (via **MCP**) and a logged-in **browser**, and posts the result — text or a
file — back. There's also a one-shot **CLI**.

```
/claude summarise the README     →  ▶ running on claude…  →  <the summary>
/codex write a haiku about protobuf
/grok what changed in the news today
/opencode refactor this function            (runs on z.ai / GLM)
/gpt explain hexagonal architecture        (the api backend)
/shot https://example.com                   (screenshots the page, posts the PNG)
/shot:work https://your-dashboard           (uses your logged-in "work" profile)
just a bare message                          (uses the default backend)
```

james is the harness, not a new agent framework: the agents *are* the backends,
their tools come from MCP, and the browser is a capability. It stays **thin in
scope but native in structure** — a single proto contract is the spine, routing
lives in one readable function, and a new backend is a single row in a table.

## The subscriptions-or-API stance

james does not resell your AI access — it uses what you already pay for.

- **`cli` backends shell out to the vendor's own command-line tool**, using that
  tool's own login. `claude` runs through the official Claude CLI on your Max
  subscription (we explicitly drop `ANTHROPIC_API_KEY` so it never falls back to
  a metered key); `codex` uses your ChatGPT login; `grok` uses your SuperGrok
  login; `opencode` runs on your z.ai (GLM) plan via its own login. Claude OAuth
  is **never** routed through a third-party tool — james only ever invokes the
  official `claude` binary.
- **`api` backends call an OpenAI-compatible HTTP endpoint** with a key read from
  the environment (`OPENAI_API_KEY`). Point it at any compatible provider.
- **`a2a` backends talk to another *agent* over the [A2A protocol](https://a2a-protocol.org/latest/)**
  (Agent2Agent), via the official [`a2a-sdk`](https://pypi.org/project/a2a-sdk/) —
  **gRPC-primary, HTTP (JSON-RPC) fallback**, transport negotiated from the
  peer's Agent Card (A2A v1.0 with v0.3 compatibility). A row points at a peer's
  A2A URL; james sends the prompt and returns the reply (files inline). The
  peer's bearer token comes from the environment by name — and it's *not* a
  harness secret, so james only ever sends that one token to that one peer,
  never your other keys. Ships with an **openclaw** row (reachable via its
  [a2a-gateway plugin](https://github.com/win4r/openclaw-a2a-gateway), which
  serves gRPC on port+1) and a disabled **hermes** placeholder (hermes has no
  A2A endpoint yet). `/openclaw <prompt>` then reaches that agent.

The prompt is fed to a CLI on **stdin** by default (avoiding flag-injection from
a leading `-` and argv length limits); a backend that only accepts an argument
uses a `{prompt}` placeholder instead (that's how `grok --single=` is wired).

## Proto-native, hexagonal design

The interface is a proto — the **internal** contract, not just an external API.
[`api/proto/james/v1/james.proto`](api/proto/james/v1/james.proto) defines
`DispatchService`. We compile it once to a descriptor and let
[invariant-protocol](https://github.com/jim-technologies/invariantprotocol)
project that descriptor to CLI / HTTP / MCP — there are **no hand-written
servers or transport code**.

```
app  →  apis  →  biz  →  (ports)  →  infra        (call flow)
imports stop at biz — infra is injected at the composition root
```

- **`biz/`** is the heart and holds almost all the logic.
  [`biz/dispatch.py`](biz/dispatch.py) reads top-to-bottom: resolve the backend,
  branch once on its kind, run it through an injected port, normalise the result.
  [`biz/backends.py`](biz/backends.py) is the registry — **adding a backend is
  one row**, and dispatch code does not change.
- **`apis/`** is a thin, translational servicer that delegates all validation
  and routing to biz.
- **`infra/`** is thin and stupid: a subprocess runner, an HTTP chat client, the
  a2a-sdk client, the JSON session store, the Telegram/Discord channel adapters,
  and the web dashboard's ASGI gate. **Channels never decide routing or talk to
  a backend** — they build a `DispatchRequest`, invoke the service, and render
  the reply. `biz` never imports `infra` (infra is injected at the composition
  root); the ports between them exchange only primitives.

The `api` seam is proved by a generic, OpenAI-compatible chat client
([`infra/clients/chat.py`](infra/clients/chat.py)) driven by
[`chat.proto`](api/proto/james/chat/v1/chat.proto) via invariant-protocol's
`connect_http` — its REST mapping comes from the `google.api.http` annotation,
not from code.

## Browser: the `shot` backend

`/shot <url>` screenshots a web page and posts the PNG back. It's **just another
cli row** (`chromium --headless --screenshot`, no browser library — within the
"shell out, no community wrappers" rule). The only shared plumbing is a media
return path: `DispatchResponse` carries `Artifact`s (raw bytes — no host path
crosses the wire), the runner reads the file the backend wrote via an
`{outfile}` placeholder, and the channels upload it (Telegram `sendPhoto`,
Discord attachment).

**Logged-in sessions.** Each `--user-data-dir` is a separate, persistent Chrome
profile (a distinct identity — "work", "personal", a client account…). Seed one
once, then reuse it headlessly:

```bash
flox activate -- ./james login work   # opens Chrome on "work" to sign in, once
/shot:work https://your-dashboard     # then: screenshots as "work" (from chat)
/shot https://example.com             # default profile (persistent, logged-out)
```

Profiles are config-driven (subdirs of `browser.profiles_dir`) — add one by
signing in, no code change. Same-profile runs serialize (Chrome locks a profile
to one process); different profiles run in parallel, bounded by `max_concurrency`.
Profile names are validated (no path traversal), and each profile directory holds
live logins — **treat it as a secret** (see Security).

## Tools: MCP

The agents are already MCP clients, so "give james your work tools" is
configuration, not a client built into james. Keep **one canonical config** and
project it to each agent — the same "define once, project to many" idea as the
proto spine, here via [invariantmcp](https://github.com/jim-technologies/invariantmcp).

1. **Define** your servers once in
   [`mcp/servers.mcp.json`](mcp/servers.mcp.json) (Claude Code `.mcp.json`
   format). Put **no secrets** in it — omit `env` for token vars so each server
   inherits them from james's (sops-decrypted) environment.
2. **Project** to the other agents:
   ```bash
   CGO_ENABLED=0 go install github.com/jim-technologies/invariantmcp@latest  # once
   flox activate -- make mcp        # → ~/.codex/config.toml (merges by server id)
   ```
3. **Per agent:**
   - **claude** — set `mcp.config_path: mcp/servers.mcp.json` in config.yaml;
     james passes it as `--mcp-config` (avoiding the `claude -p` project-approval
     gate, which would otherwise leave a dropped-in `.mcp.json` unused). For
     unattended *tool execution* prefer `--permission-mode acceptEdits` or a tool
     allowlist on the claude row. **Avoid `--dangerously-skip-permissions`:** it
     removes the last guardrail on an agent that already has the full env and a
     repo-root cwd, turning a prompt injection into arbitrary code execution — if
     you must, pair it with a non-repo `working_dir`, egress limits, and the
     broker secrets tier (below).
   - **codex** — reads the `~/.codex/config.toml` that `make mcp` writes.
   - **grok** — not an invariantmcp target yet; add servers with `grok mcp add …`.

No MCP client lives in james — it stays a thin, self-sufficient harness; the
agents bring the tools.

## Secrets the agents can use (e.g. a database)

When an agent needs a credential to *do* something (query a DB, call an
internal API), the rule is **give it a capability, never the credential** — and
**never put the secret in the prompt** (prompts go to Anthropic/OpenAI). Tiers,
weakest to strongest:

- **Never:** pasting a password into the message. It's sent to the provider.
- **Baseline (works today):** keep the secret in the SOPS+age dotenv → it's in
  james's environment and is inherited by the agent process, so the agent uses
  it through a **tool that reads the env var**, not through chat — e.g. an MCP
  server configured with the env var name (no value), or a wrapper command like
  `psql "$DATABASE_URL"`. The secret stays out of the prompt and the model's
  context. ⚠️ The agent *process* inherits **all** of james's env *except*
  james's own secrets (bot tokens + provider keys are stripped via `env_unset`),
  so what's exposed is the agent-use secret you add — a prompt-injected agent
  could surface it, so **scope it to least privilege** (a read-only role, a
  short-lived/rotatable token), never a master password.
- **Stronger:** don't put the secret in the agent's environment at all. Run a
  **broker** — a small local tool service / **HTTP MCP server** that holds the
  credential and exposes only safe operations (e.g. `db.query(sql)`). The agent
  gets only the broker's URL (and at most a scoped token); the password lives in
  the broker's env, never in the agent or the model. invariantmcp's `http`
  transport makes this a config entry, not code.
- **Best:** the broker fetches **short-lived dynamic credentials** (e.g. Vault
  DB secrets) per use, so no long-lived password exists anywhere — scoped,
  audited, revocable.

Two things to do regardless of tier:
- **Restrict outbound network.** The real exfiltration channel is egress: a
  prompt-injected agent can POST a secret/result anywhere. Lock james's outbound
  traffic to just the provider APIs, Telegram, and your DB/broker (e.g. systemd
  `IPAddressDeny=any` + `IPAddressAllow=…`, a netns, or a Nomad network policy —
  see `deploy/systemd/james.service`). It's the single strongest control.
- **Mind the working dir.** cli agents run with read/write/shell in `working_dir`
  (repo root by default) — keep `secrets.enc.env` and the age key *outside* it,
  or point `working_dir` at a dedicated workspace for untrusted-input tasks.

One honest caveat at every tier: the *data the tool returns* (query results)
still goes back to the model as context, i.e. to the provider. Keeping the
*credential* private is solved above; if the *data itself* must not reach a
provider, use a local model or redact at the broker.

## Setup

Everything runs through [flox](https://flox.dev) (the toolchain) and
[uv](https://docs.astral.sh/uv/) (Python deps).

```bash
flox activate            # python, uv, buf, protoc, sops, age, chromium (/shot)…
uv sync                  # installs Python deps into .venv (per uv.lock)
```

One-time, on the host, log in to the CLI backends you want to use:

```bash
claude     # then complete the login (Max subscription)
codex      # ChatGPT login
grok       # SuperGrok login
opencode auth login   # pick z.ai (GLM) — the `opencode` backend uses
                      # zai-coding-plan/glm-4.7; needs your z.ai plan/key
```

Session memory for codex/grok/opencode reads each CLI's own JSON output to
capture the session id, so it requires those CLIs' machine-readable output to
stay enabled (the backend rows force `--json` / `--output-format json` /
`--format json` — don't override them).

Secrets (bot tokens, API keys) are read from the **environment**, supplied at
runtime from a **SOPS + age**-encrypted dotenv — never a committed plaintext
file. See [`secrets.example.env`](secrets.example.env):

```bash
cp secrets.example.env secrets.env          # fill in values
sops --encrypt --age <your-age-recipient> secrets.env > secrets.enc.env
rm secrets.env
```

Edit [`config.yaml`](config.yaml) to enable channels and set the **fail-closed
allowlist** of chat/channel ids.

## Running

```bash
# One-shot from the terminal (same path the channels use):
flox activate -- ./james cli --backend codex "summarise the README"
flox activate -- ./james cli "what is hexagonal architecture?"   # default backend

# Run the enabled messaging channels, with secrets injected for the session:
sops exec-env secrets.enc.env 'flox activate -- ./james serve'
```

A long-running agent never blocks the bot: each task is acknowledged instantly
(`▶ running on <backend>…`) and dispatched in a bounded background task, so a
second message during a long run is handled right away.

## Memory: a session per thread

Each chat thread is its own persistent **agent session**, so james *remembers* —
follow-ups continue the conversation and you can answer the agent's questions
in-thread. The chat platform's native threading *is* the session manager:

- **Telegram:** enable **Topics** in a (private) supergroup — each topic is a
  separate session, and the topic list is your session list. A plain DM is a
  single session. Replies always land back in the originating topic.
- **`/reset`** in a thread forgets its history and starts fresh.

All agent backends remember per thread: **claude**, **codex**, **grok**, and
**opencode** (z.ai). They use two session models. claude is *caller-set* — james
mints the id and passes it (`--session-id` / `--resume`). codex/grok/opencode are
*capture* — the CLI mints its own id and prints it in its JSON output, so james
parses it from the first run, stores it, and resumes by it (`codex exec resume`,
`grok --resume`, `opencode -s`). Either way, a dead/pruned session self-heals:
james detects the CLI's "session gone" message and starts a fresh one for that
thread (memory is lost, the thread keeps working). `shot` and `james cli` stay
one-shot. Sessions live in a small JSON file (`sessions.store_path` — put it on
persistent storage for a deploy, on the **same** boundary as each CLI's own state
under `$HOME`; see [`deploy/README.md`](deploy/README.md)). Memory is the one
piece of harness state, and it's optional: set `sessions.store_path: ""` to run
**stateless** — no file is written and every message is a self-contained
one-shot.

> **Next (not yet built):** inline **[Approve] [Deny]** buttons for
> tool-permission requests, via claude's stream-json protocol — the safe,
> in-chat alternative to `--dangerously-skip-permissions`.

## Web dashboard (optional)

A small browser dashboard to list your sessions and drive any thread from a
browser — handy when you want a keyboard and a bigger window than a chat app.
It's **off by default** (`web.enabled` in `config.yaml`) and rides the *same*
`DispatchService` projection the channels use, so a web prompt goes through the
exact dispatch path and per-agent **secret-stripping** — no new secret surface.
It's a dependency-free vanilla-JS page (no framework, no build step).

```yaml
web:
  enabled: true
  bind_host: "127.0.0.1"     # loopback; front with a TLS+auth proxy for remote
  port: 8765
  username: "james"
  token_env: "JAMES_WEB_PASSWORD"   # Basic-auth password from the sops dotenv
```

Auth is **HTTP Basic**, fail-closed: an unset/empty password serves nobody. It
binds loopback by default — for remote access put it behind a TLS-terminating
authenticating reverse proxy (or reach it over an SSH tunnel); never bind it to a
public interface bare. Phase 1 is **send + list**: a freshly opened thread shows
no prior transcript (only the round-trips you make in that page session) — full
history rendering is a deferred phase. Real **terminal access** (a live TUI in
the browser) was deliberately *not* built: it can't ride the proto projection,
would re-expose the stripped secrets, and concurrent use corrupts a thread's
memory — the dashboard gives the same reach without those hazards.

## Embedding james in your application

james is a library before it is a daemon: the composition root is importable
([`app/wiring.py`](app/wiring.py)), so an application can build the service and
drive it **in-process** — no channels, no HTTP, no checkout-layout assumptions:

```python
from app.config import load_config
from app.wiring import build_server, build_session_store

config = load_config(config_path)          # any path; see also $JAMES_CONFIG
root = config_path.parent                  # relative config paths resolve here
server = build_server(config, root=root,
                      store=build_session_store(config, root))
response = await server.invoke("DispatchService.Dispatch", request)
```

- **Stateless by choice:** `sessions.store_path: ""` disables the store — the
  only harness state — so every dispatch is a pure (backend, prompt) → Result
  call. Or inject your own `SessionStore` (a 4-method Protocol) to keep
  conversation memory in your app's database.
- **Out-of-process instead:** the same service is one config line away over
  HTTP (`server.http_port`, Connect-JSON:
  `POST /james.v1.DispatchService/Dispatch` / `ListSessions` / `ListBackends`,
  plus `GET /healthz`), so any language can treat james as a sidecar. ⚠ That
  port binds all interfaces with **no auth** — keep it firewalled, or use the
  web dashboard port instead: it proxies the same RPCs behind fail-closed
  Basic auth on loopback (`curl -u james:$PASSWORD
  http://127.0.0.1:8765/james.v1.DispatchService/Dispatch …`).
- The CLI itself is just this seam plus argument parsing: `./james --config
  /etc/james/config.yaml serve` (or `$JAMES_CONFIG`) runs against any config
  location.

## Deployment

james runs as one long-lived `james serve` process. [`deploy/`](deploy/) has
ready-to-use patterns for **flox** (manifest service), **Nix/NixOS**
([`flake.nix`](flake.nix) dev shell + module), **Nomad**
([`deploy/Dockerfile`](deploy/Dockerfile) + [job spec](deploy/nomad/james.nomad.hcl)),
and **Ansible** (systemd role). Rule of thumb: `api` backends run anywhere (just
an env key), while `cli` backends (claude/codex/grok/opencode) need the vendor
CLI logged in on the host — so they suit a host install (systemd / Ansible /
NixOS) over a container. Full guide: [`deploy/README.md`](deploy/README.md).

## Security

Access is **fail-closed**: a chat/channel id must be in the `config.yaml`
allowlist or the message is silently ignored — an empty allowlist serves nobody.
No secrets live in git; tokens and keys come only from the environment.

Note on `/shot`: it fetches whatever URL an allowlisted user sends, **from the
server**, so it can reach internal addresses (SSRF) — the chat allowlist is the
trust boundary. Chromium runs with `--no-sandbox` (standard for headless
servers/containers); run james as an unprivileged user. If you expose `/shot` to
less-trusted chats, add a URL allowlist / block private IP ranges first.

Note on the web dashboard: it's off by default, binds loopback, and is gated by
fail-closed HTTP Basic auth (empty password ⇒ serves nobody). It rides the same
`dispatch` path, so it inherits the agent secret-stripping and exposes no new
secrets. Anyone who reaches it can drive your agents, so for remote access put it
behind a TLS-terminating authenticating reverse proxy (or an SSH tunnel) — never
bind it to a public interface bare.

Note on browser profiles: a logged-in profile lets james act **as you** on every
site it's signed into — each profile directory is a **credential**. Keep
`browser.profiles_dir` on a secured, persistent volume owned by the service user,
never in git (it's gitignored), and keep the allowlist tight — anyone who can
message james can browse as any of those identities. The more profiles, the
larger the aggregate blast radius; keep the browser single-tenant to james.

## Development

```bash
flox activate -- make ci      # exactly what CI runs
```

`ci` = `fmt-check` + `lint` + `typecheck` + `audit` + `test` + `gen-check`.
Tests are two-tier: unit tests always run with no network or keys (dependencies
are injected, never monkeypatched); live tests are gated behind
`RUN_LIVE_TESTS=1`. The generated proto code under `gen/` is committed, and
`gen-check` fails the build if it drifts from the `.proto` sources. To regenerate
after editing a proto:

```bash
flox activate -- make -C api generate
```

## Roadmap

Built: messaging channels, the proto-native dispatch core, cli + api backends,
the browser backend with logged-in profiles, MCP tools (codex/grok via their
CLIs; claude via `--mcp-config`), per-thread session memory for **all** agent
backends (claude caller-set; codex/grok/opencode capture-from-output, with
dead-session self-heal), an optional **web dashboard** (send + list, riding the
proto projection), and an **A2A client** (`a2a` backends to talk to other agents,
e.g. openclaw). Still designed-for seams, not yet built: in-chat tool-approval
buttons (claude stream-json), web-dashboard transcript history, A2A session
continuity (contextId) + exposing james *as* an A2A server, scheduling &
proactivity, multi-agent fan-out, streaming partial output, and a job queue. No
agent framework is or will be a dependency — james stays a thin harness.

## License

MIT — see [LICENSE](LICENSE).
