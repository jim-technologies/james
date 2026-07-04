# Developer entry points. Run inside the flox env, e.g. `flox activate -- make ci`.
# CI runs exactly that, so `flox activate -- make ci` reproduces CI locally.

fmt:
	uv run ruff format .
	$(MAKE) -C api format

fmt-check:
	uv run ruff format --check .
	$(MAKE) -C api format-check

lint:
	uv run ruff check .
	$(MAKE) -C api lint

lint-fix:
	uv run ruff check --fix .

typecheck:
	uv run ty check

audit:
	uv run pip-audit

test:
	uv run pytest -q          # RUN_LIVE_TESTS=1 to also run live tests

check: fmt-check lint typecheck audit test

# Regenerate proto code, then fail if anything under gen/ changed (drift).
gen-check:
	$(MAKE) -C api generate
	@if [ -n "$$(git status --porcelain -- gen/)" ]; then \
	  echo "gen/ is stale — run 'make -C api generate' and commit:"; \
	  git status --porcelain -- gen/; \
	  exit 1; \
	fi

ci: check gen-check

# Project the canonical MCP config (mcp/servers.mcp.json, Claude Code format) to
# the other agents via invariantmcp. claude reads it directly through
# --mcp-config (set mcp.config_path); codex gets ~/.codex/config.toml here.
# invariantmcp detects format by filename, so copy to a temp `.mcp.json` first.
# Install once: CGO_ENABLED=0 go install github.com/jim-technologies/invariantmcp@latest
CODEX_CONFIG ?= $(HOME)/.codex/config.toml
mcp:
	@command -v invariantmcp >/dev/null 2>&1 || { \
	  echo "invariantmcp not found — install it with:"; \
	  echo "  CGO_ENABLED=0 go install github.com/jim-technologies/invariantmcp@latest"; \
	  exit 1; }
	@set -e; trap 'rm -rf .mcp-build' EXIT; \
	  mkdir -p .mcp-build && cp mcp/servers.mcp.json .mcp-build/.mcp.json; \
	  invariantmcp --cli ConfigService Convert \
	    -r '{"in":".mcp-build/.mcp.json","out":"$(CODEX_CONFIG)"}'
	@echo "MCP synced to codex ($(CODEX_CONFIG)). grok: not an invariantmcp"
	@echo "target — add servers with 'grok mcp add ...'."

.PHONY: fmt fmt-check lint lint-fix typecheck audit test check gen-check ci mcp
