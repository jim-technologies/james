# Self-contained james image. A container is already a pinned environment, so
# this uses a python+uv base directly rather than flox; `flox containerize` is
# the flox-native alternative (toolchain only — see deploy/README.md).
#
# Out of the box this serves `api` backends (set OPENAI_API_KEY), `a2a`
# backends (set the peer token), and the `shot` backend (headless chromium,
# installed below). The `cli` backends (claude/codex/grok/opencode) are NOT
# installed here and need an interactive vendor login — to use them in a
# container, add the vendor CLIs to this image and mount their login-state dirs
# as volumes (see deploy/README.md).
#
# Build (from the repo root):  docker build -f deploy/Dockerfile -t james:latest .
FROM python:3.13-slim

# uv from the official image (pinned to match the repo's uv.lock toolchain).
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

# git: invariant-protocol is a git dependency in uv.lock. chromium + fonts: the
# `shot` backend (web screenshots). Drop chromium/fonts to slim the image if you
# do not use `shot`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates chromium fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/james
COPY . .

# Install runtime deps into a project-local .venv from the committed lockfile.
RUN uv sync --frozen --no-dev

ENV PYTHONPATH=/opt/james:/opt/james/gen/python \
    PYTHONUNBUFFERED=1

# Run as a non-root user; any cli-backend login state lives under its home.
RUN useradd --create-home --uid 1000 james && chown -R james:james /opt/james
USER james

# Same invoke path as the launcher: `serve` (default) or `cli --backend ... "..."`.
ENTRYPOINT ["uv", "run", "--no-dev", "python", "-m", "app"]
CMD ["serve"]
