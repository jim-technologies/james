# james — Nomad job (docker driver). Runs the self-contained image built from
# deploy/Dockerfile. Placeholders (REGISTRY, datacenter, Vault path) are marked;
# adjust to your cluster. This example serves the `api` backend (gpt) over
# Telegram — fully self-contained. For `cli` backends see the note at the bottom.
#
#   docker build -f deploy/Dockerfile -t REGISTRY/james:0.1.0 . && docker push ...
#   nomad job run deploy/nomad/james.nomad.hcl

job "james" {
  datacenters = ["dc1"] # <- your datacenter(s)
  type        = "service"

  group "james" {
    count = 1

    restart {
      attempts = 3
      interval = "5m"
      delay    = "15s"
      mode     = "delay"
    }

    task "serve" {
      driver = "docker"

      config {
        image = "REGISTRY/james:0.1.0" # <- your image reference
        args  = ["serve"]

        # Bind the templated config.yaml (below) over the image's default.
        mount {
          type     = "bind"
          source   = "local/config.yaml"
          target   = "/opt/james/config.yaml"
          readonly = true
        }
      }

      # Secrets: render bot tokens / API keys from Vault into the task env.
      # Never bake secrets into the image. (Swap for Nomad variables if you
      # don't run Vault: {{ with nomadVar "nomad/jobs/james" }}…{{ end }}.)
      template {
        destination = "secrets/james.env"
        env         = true
        change_mode = "restart"
        data        = <<-EOH
          {{- with secret "secret/data/james" -}}
          OPENAI_API_KEY={{ .Data.data.openai_api_key }}
          TELEGRAM_BOT_TOKEN={{ .Data.data.telegram_bot_token }}
          {{- end }}
        EOH
      }

      # Mount config.yaml into the image's working dir. Keep secrets OUT of here.
      template {
        destination = "local/config.yaml"
        change_mode = "restart"
        data        = <<-EOH
          default_backend: gpt
          working_dir: "."
          # Per-thread session memory. /alloc/data survives task restarts within
          # an allocation but NOT a reschedule — mount a host_volume (see the
          # note at the bottom) and point this at it for memory that outlives a
          # reschedule. The in-image default (./.james-sessions.json) is on the
          # ephemeral writable layer and is lost on every restart.
          sessions:
            store_path: "/alloc/data/sessions.json"
          channels:
            telegram:
              enabled: true
              token_env: TELEGRAM_BOT_TOKEN
              allowed_chat_ids: []   # <- your allowlisted chat ids (fail-closed)
            discord:
              enabled: false
              token_env: DISCORD_BOT_TOKEN
              allowed_channel_ids: []
        EOH
      }

      resources {
        cpu    = 500  # MHz
        # 512MB suits api-only. The `shot` backend runs headless Chromium —
        # give it ~1-2GB headroom or a hostile/heavy page can OOM the task.
        memory = 512  # MB
      }
    }
  }
}

# ── cli backends (claude / codex / grok / opencode) in Nomad ──────────────────
# The image above does NOT contain the vendor CLIs or their interactive logins.
# To run cli backends in a container you must (1) add the vendor CLIs to
# deploy/Dockerfile, and (2) persist their login state (~/.claude, ~/.codex,
# ~/.grok) across restarts via a host volume mounted at the james user's home.
# For cli-heavy deployments, prefer the host install (systemd / Ansible / the
# NixOS module) where the host already has the CLIs logged in. See deploy/README.md.
#
# ── session memory (per-thread agent context) ─────────────────────────────────
# The config above writes the session store to /alloc/data/sessions.json, which
# survives task restarts within an allocation but not a reschedule to another
# node. For memory that outlives a reschedule, register a host_volume on the
# clients and mount it, then point sessions.store_path at the mount, e.g.:
#   group "james" {
#     volume "state" { type = "host"  source = "james-state"  read_only = false }
#     task "serve" {
#       volume_mount { volume = "state"  destination = "/var/lib/james" }
#     }
#   }
# and set sessions.store_path: "/var/lib/james/sessions.json" in the config.
# (For cli backends, claude's own sessions live under ~/.claude — persist HOME on
# the SAME volume boundary, or a redeploy strands threads on dead resumes; james
# now self-heals a dead resume by starting a fresh session, but memory is lost.)
#
# ── browser profiles (the `shot` backend) ─────────────────────────────────────
# `shot` works out of the box with a persistent, logged-out default profile. For
# LOGGED-IN profiles, mount a host volume at the image's browser.profiles_dir and
# seed each profile on a workstation first (logins need a display) — copy the
# profile dir onto the volume. Give the task ~1-2GB memory if `shot` runs
# (headless Chromium).
