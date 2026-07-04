"""CLI entry point and wiring for james.

Wires the layers: build the infra adapters, build the invariant-protocol Server
from the committed descriptor, register the DispatchService servicer, then run
the enabled channels (``serve``) or perform a one-shot dispatch (``cli``).
Both paths go through the same service invoke, so channels and the CLI share one
routing path and neither talks to a backend directly.
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

from apis.dispatch_server import DispatchServiceImpl
from app.config import ChannelConfig, Config, load_config
from infra.backends.cli import SubprocessCliRunner
from infra.channels.discord import DiscordChannel
from infra.channels.telegram import TelegramChannel
from infra.clients.a2a import A2ASdkCaller
from infra.clients.chat import ChatApiCaller
from infra.sessions.store import JsonSessionStore

_ROOT = Path(__file__).resolve().parent.parent
_DESCRIPTOR = _ROOT / "gen" / "descriptor.binpb"
_CONFIG = _ROOT / "config.yaml"


def _session_store(config: Config) -> JsonSessionStore:
    """Build the per-conversation session store at the configured path."""
    return JsonSessionStore(str((_ROOT / config.session_store_path).resolve()))


def _build_server(config: Config, store: JsonSessionStore) -> Server:
    """Build the Server and register the DispatchService servicer."""
    mcp_config = (
        str((_ROOT / config.mcp_config_path).resolve())
        if config.mcp_config_path
        else ""
    )
    servicer = DispatchServiceImpl(
        cli_runner=SubprocessCliRunner(),
        api_caller=ChatApiCaller(str(_DESCRIPTOR)),
        a2a_caller=A2ASdkCaller(),
        default_backend=config.default_backend,
        cwd=str((_ROOT / config.working_dir).resolve()),
        profiles_dir=str((_ROOT / config.browser.profiles_dir).resolve()),
        default_profile=config.browser.default_profile,
        mcp_config=mcp_config,
        session_store=store,
    )
    server = Server.from_descriptor(str(_DESCRIPTOR))
    server.register(servicer)
    return server


def _require_token(channel: ChannelConfig, name: str) -> str:
    """Read a channel's bot token from its configured env var, or exit."""
    token = os.environ.get(channel.token_env, "")
    if not token:
        print(f"{name}: ${channel.token_env} is not set", file=sys.stderr)
        raise SystemExit(2)
    return token


async def _serve(config: Config) -> None:
    """Run every enabled channel (and optional HTTP projection) concurrently."""
    store = _session_store(config)
    # A store inside the checkout is wiped by a redeploy that re-clones or
    # rebuilds, silently resetting all thread memory. Fine for dev; flag it when
    # serving so a deploy that left the default surfaces in the logs (point it
    # at durable storage, e.g. /var/lib/james/sessions.json — see deploy/).
    store_path = (_ROOT / config.session_store_path).resolve()
    if store_path.is_relative_to(_ROOT):
        print(
            f"warning: session store {store_path} is inside the checkout and "
            "will be lost on redeploy; set sessions.store_path to durable "
            "storage (e.g. /var/lib/james/sessions.json).",
            file=sys.stderr,
        )
    server = _build_server(config, store)

    async def invoke(
        request: james_pb2.DispatchRequest,
    ) -> james_pb2.DispatchResponse:
        return await server.invoke("DispatchService.Dispatch", request)

    coros = []
    channels = []
    if config.telegram.enabled:
        channel = TelegramChannel(
            token=_require_token(config.telegram, "telegram"),
            allowed_chat_ids=set(config.telegram.allowed_ids),
            invoker=invoke,
            default_backend=config.default_backend,
            max_concurrency=config.max_concurrency,
            reset_session=store.reset,
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
            reset_session=store.reset,
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


async def _cli(config: Config, backend: str, prompt: str) -> int:
    """Run a single dispatch through the service and print the result."""
    server = _build_server(config, _session_store(config))
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


def _login(config: Config, profile: str) -> int:
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
    profiles_root = (_ROOT / config.browser.profiles_dir).resolve()
    profile_dir = profiles_root / safe
    profile_dir.mkdir(parents=True, exist_ok=True)
    # The profile holds live logins (a credential) — keep it private even if the
    # directory already existed at a looser mode.
    with contextlib.suppress(OSError):
        profiles_root.chmod(0o700)
        profile_dir.chmod(0o700)
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

    config = load_config(_CONFIG)
    if args.mode == "serve":
        asyncio.run(_serve(config))
        return 0
    if args.mode == "login":
        return _login(config, args.profile)
    return asyncio.run(_cli(config, args.backend, " ".join(args.prompt)))


if __name__ == "__main__":
    raise SystemExit(main())
