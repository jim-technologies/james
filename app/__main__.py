"""CLI entry point for james.

Thin argument parsing over the importable composition root (``app.wiring``):
load the config, build the Server, then run the enabled channels (``serve``)
or perform a one-shot dispatch (``cli``). Both paths go through the same
service invoke, so channels and the CLI share one routing path and neither
talks to a backend directly. The config file is the repo's config.yaml by
default; override with --config or $JAMES_CONFIG (relative paths inside the
config resolve against the config file's directory).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from invariant import Server
from james.v1 import james_pb2

from app.config import ChannelConfig, Config, load_config
from app.wiring import build_server, build_session_store
from infra.channels.discord import DiscordChannel
from infra.channels.telegram import TelegramChannel

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _ROOT / "config.yaml"


def _require_token(channel: ChannelConfig, name: str) -> str:
    """Read a channel's bot token from its configured env var, or exit."""
    token = os.environ.get(channel.token_env, "")
    if not token:
        print(f"{name}: ${channel.token_env} is not set", file=sys.stderr)
        raise SystemExit(2)
    return token


async def _serve(config: Config, root: Path) -> None:
    """Run every enabled channel (and optional HTTP projection) concurrently."""
    store = build_session_store(config, root)
    # A store inside the CODE CHECKOUT (_ROOT) is wiped by a redeploy that
    # re-clones or rebuilds, silently resetting all thread memory. Fine for
    # dev; flag it when serving so a deploy that left the default surfaces in
    # the logs (point it at durable storage, e.g. /var/lib/james/sessions.json
    # — see deploy/). A store elsewhere (e.g. /etc/james) is durable — no
    # warning, wherever the config file lives.
    if store is not None:
        store_path = (root / config.session_store_path).resolve()
        if store_path.is_relative_to(_ROOT):
            print(
                f"warning: session store {store_path} is inside the checkout "
                "and will be lost on redeploy; set sessions.store_path to "
                "durable storage (e.g. /var/lib/james/sessions.json).",
                file=sys.stderr,
            )
    server = build_server(config, root=root, store=store)

    async def invoke(
        request: james_pb2.DispatchRequest,
    ) -> james_pb2.DispatchResponse:
        return await server.invoke("DispatchService.Dispatch", request)

    coros = []
    channels = []
    reset = store.reset if store is not None else None
    if config.telegram.enabled:
        channel = TelegramChannel(
            token=_require_token(config.telegram, "telegram"),
            allowed_chat_ids=set(config.telegram.allowed_ids),
            invoker=invoke,
            default_backend=config.default_backend,
            max_concurrency=config.max_concurrency,
            reset_session=reset,
        )
        channels.append(channel)
        coros.append(channel.run())
    if config.discord.enabled:
        channel = DiscordChannel(
            token=_require_token(config.discord, "discord"),
            allowed_channel_ids=set(config.discord.allowed_ids),
            invoker=invoke,
            default_backend=config.default_backend,
            max_concurrency=config.max_concurrency,
            reset_session=reset,
        )
        channels.append(channel)
        coros.append(channel.run())
    if config.web.enabled:
        password = os.environ.get(config.web.token_env, "")
        if not password:
            print(
                f"web: ${config.web.token_env} not set; web UI disabled",
                file=sys.stderr,
            )
        else:
            coros.append(_serve_web(server, config, password))
    if config.http_port:
        coros.append(server.serve(http=config.http_port))

    if not coros:
        print(
            "No channels enabled. Edit config.yaml (channels.*.enabled).",
            file=sys.stderr,
        )
        return
    print(f"james serving: {_enabled_summary(config)}", file=sys.stderr)
    try:
        await asyncio.gather(*coros)
    finally:
        for channel in channels:
            await channel.aclose()
        await server.stop()


async def _serve_web(server: Server, config: Config, password: str) -> None:
    """Serve the web dashboard on loopback: Basic auth over the proto app.

    Wraps server.asgi_app() so a web prompt rides the same DispatchService path
    (and secret-stripping) as the channels. Bound to bind_host (loopback by
    default); front it with a TLS+auth reverse proxy for remote access.
    """
    import uvicorn

    from infra.web.app import WebApp

    app = WebApp(
        server.asgi_app(),
        username=config.web.username,
        password=password,
        static_dir=str(_ROOT / "web"),
    )
    uv = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.web.bind_host,
            port=config.web.port,
            log_level="warning",
        )
    )
    await uv.serve()


def _enabled_summary(config: Config) -> str:
    """Describe which projections are running, for a startup log line."""
    parts = []
    if config.telegram.enabled:
        parts.append("telegram")
    if config.discord.enabled:
        parts.append("discord")
    if config.web.enabled:
        parts.append(f"web:{config.web.bind_host}:{config.web.port}")
    if config.http_port:
        parts.append(f"http:{config.http_port}")
    return ", ".join(parts) or "(nothing)"


async def _cli(config: Config, root: Path, backend: str, prompt: str) -> int:
    """Run a single dispatch through the service and print the result."""
    # One-shot: conversation_id is empty, so no store is needed (stateless).
    server = build_server(config, root=root)
    try:
        response = await server.invoke(
            "DispatchService.Dispatch",
            james_pb2.DispatchRequest(
                backend=backend,
                prompt=prompt,
                channel="cli",
                conversation_id="",  # one-shot: no session resume
            ),
        )
    finally:
        await server.stop()
    if response.ok:
        if response.text:
            print(response.text)
        for artifact in response.artifacts:
            suffix = os.path.splitext(artifact.filename)[1]
            fd, path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as handle:
                handle.write(artifact.content)
            print(f"saved {artifact.mime or 'file'} -> {path}")
        return 0
    print(f"error [{response.backend}]: {response.error}", file=sys.stderr)
    return 1


def _login(config: Config, root: Path, profile: str) -> int:
    """Open a headful Chrome on a browser profile so you can sign in once.

    After you sign in and close the window, the `shot` backend reuses that
    profile headlessly. Needs a display — on a headless host run it over
    VNC/X-forwarding.
    """
    safe = profile.strip().lower()
    if not safe or not all(c.isalnum() or c in "-_" for c in safe):
        print(f"invalid profile name: {profile!r}", file=sys.stderr)
        return 2
    chromium = shutil.which("chromium")
    if chromium is None:
        print("chromium not found on PATH", file=sys.stderr)
        return 127
    profiles_root = (root / config.browser.profiles_dir).resolve()
    profile_dir = profiles_root / safe
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The profile holds live logins (a credential) — keep it private even if
    # the directory already existed at a looser mode. Profile first, root
    # second, each in its own suppress: a failure on one (e.g. EPERM on an
    # unowned root) must not skip tightening the other.
    with contextlib.suppress(OSError):
        profile_dir.chmod(0o700)
    with contextlib.suppress(OSError):
        profiles_root.chmod(0o700)
    print(
        f"Opening Chrome on profile '{safe}'. Sign in, then close the window.\n"
        f"profile dir: {profile_dir}",
        file=sys.stderr,
    )
    return subprocess.call(  # noqa: S603 (known tool, sanitized profile dir)
        [chromium, f"--user-data-dir={profile_dir}", "about:blank"]
    )


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the requested mode."""
    parser = argparse.ArgumentParser(
        prog="james", description="A personal AI chief-of-staff."
    )
    parser.add_argument(
        "--config",
        # `or`, not a get() default: a set-but-empty JAMES_CONFIG (common in
        # env files) must fall back too, not resolve "" to the cwd.
        default=os.environ.get("JAMES_CONFIG") or str(_CONFIG),
        help=(
            "path to config.yaml (default: the repo's, or $JAMES_CONFIG); "
            "relative paths inside it resolve against its directory"
        ),
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("serve", help="run the enabled messaging channels")
    cli = sub.add_parser("cli", help="one-shot dispatch from the terminal")
    cli.add_argument(
        "--backend",
        default="",
        help="backend name (default: the config default)",
    )
    cli.add_argument("prompt", nargs="+", help="the task / prompt text")
    login = sub.add_parser("login", help="sign in to a browser profile (shot)")
    login.add_argument(
        "profile", help="profile name (a browser.profiles_dir sub-dir)"
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    root = config_path.parent
    if args.mode == "serve":
        asyncio.run(_serve(config, root))
        return 0
    if args.mode == "login":
        return _login(config, root, args.profile)
    return asyncio.run(_cli(config, root, args.backend, " ".join(args.prompt)))


if __name__ == "__main__":
    raise SystemExit(main())
