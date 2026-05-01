"""Typer CLI entry point.

Subcommands:
- ``agora mcp``           — run the outer MCP server over stdio.
- ``agora watch``         — attach to a project room and stream events into the terminal.
- ``agora setup-ollama``  — VRAM-check then pull the default Ollama model.
- ``agora doctor``        — probe Ollama, claude CLI, GPU, and report status.
- ``agora version``       — print the package version.
"""

from __future__ import annotations

import asyncio

import typer

from agora import __version__
from agora.config import get_settings

app = typer.Typer(help="Agora multi-agent orchestration CLI")


@app.command()
def version() -> None:
    """Print the Agora version."""
    typer.echo(__version__)


@app.command()
def mcp() -> None:
    """Run the outer MCP server over stdio."""
    from agora.fleet.orchestrator import Orchestrator
    from agora.fleet.vram import check_model_fits, raise_if_wont_fit
    from agora.matrix.client import AgoraMatrixClient
    from agora.matrix.room_manager import RoomManager
    from agora.mcp.handlers import AgoraHandlers
    from agora.mcp.server import run_stdio

    settings = get_settings()

    async def _main() -> None:
        client = AgoraMatrixClient(
            homeserver=settings.matrix_homeserver, user_id=settings.matrix_user_id
        )
        if settings.matrix_password:
            await client.login(settings.matrix_password)

        room_manager = RoomManager(client, homeserver_name=settings.matrix_server_name)

        def llm_factory(model: str):
            return _build_adapter(model, settings)

        vram_gate = None
        if not settings.skip_vram_check:

            async def _gate(model: str) -> None:
                if not model.startswith("ollama/"):
                    return
                check = await check_model_fits(
                    model=model,
                    base_url=settings.ollama_base_url,
                    safety_margin_mib=settings.vram_safety_margin_mib,
                )
                raise_if_wont_fit(check, model)

            vram_gate = _gate

        orchestrator = Orchestrator(
            matrix_client=client,
            room_manager=room_manager,
            llm_factory=llm_factory,
            work_dir=str(settings.work_dir),
            homeserver_name=settings.matrix_server_name,
            max_parallel_agents=settings.max_parallel_agents,
            vram_check=vram_gate,
            enable_observer=settings.enable_observer,
            repo_root=str(settings.git_repo_path),
            knowledge_cache_dir=str(settings.knowledge_cache_dir),
            ollama_base_url=settings.ollama_base_url,
            skip_warmup=settings.skip_llm_warmup,
            warmup_deadline=settings.llm_warmup_seconds,
            review_timeout_seconds=settings.review_timeout_seconds,
            enable_web_fetch=settings.enable_web_fetch,
            fetch_timeout_seconds=settings.fetch_timeout_seconds,
            fetch_max_bytes=settings.fetch_max_bytes,
            fetch_max_text_bytes=settings.fetch_max_text_bytes,
        )
        handlers = AgoraHandlers(orchestrator, flows_dir=settings.flows_dir)
        try:
            await run_stdio(handlers)
        finally:
            await client.close()

    asyncio.run(_main())


@app.command("setup-ollama")
def setup_ollama(
    model: str = typer.Option(None, help="Model to pull (defaults to settings.llm_model)"),
) -> None:
    """Probe VRAM, pull the model, and warm it up so first turns are fast."""
    import aiohttp

    from agora.core.errors import AgoraError
    from agora.fleet.vram import check_model_fits, raise_if_wont_fit

    settings = get_settings()
    target = model or settings.llm_model
    if target.startswith("ollama/"):
        target_name = target.removeprefix("ollama/")
    else:
        target_name = target

    async def _setup() -> None:
        typer.echo(f"→ Checking Ollama at {settings.ollama_base_url}...")
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5, sock_connect=2)
            ) as s, s.get(f"{settings.ollama_base_url}/api/tags") as resp:
                if resp.status != 200:
                    raise AgoraError(f"Ollama returned HTTP {resp.status}")
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise AgoraError(
                f"Cannot reach Ollama at {settings.ollama_base_url} — run `ollama serve`?"
            ) from exc
        typer.echo("  ok.")

        if settings.skip_vram_check:
            typer.echo("→ VRAM check skipped (AGORA_SKIP_VRAM_CHECK=1).")
        else:
            typer.echo(f"→ VRAM check for {target_name}...")
            check = await check_model_fits(
                model=target_name,
                base_url=settings.ollama_base_url,
                safety_margin_mib=settings.vram_safety_margin_mib,
            )
            if check.free_mib is None:
                typer.echo(f"  {check.reason}")
            else:
                typer.echo(
                    f"  free={check.free_mib} MiB, needed≈{check.required_mib} MiB"
                )
            raise_if_wont_fit(check, target_name)
            typer.echo("  ok.")

        typer.echo(f"→ Pulling {target_name}...")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3600, sock_connect=10)
        ) as s, s.post(
            f"{settings.ollama_base_url}/api/pull",
            json={"name": target_name, "stream": False},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise AgoraError(f"Pull failed HTTP {resp.status}: {body[:200]}")
        typer.echo("  ok.")

        typer.echo("→ Warm-up (keep_alive=30m)...")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, sock_connect=5)
        ) as s, s.post(
            f"{settings.ollama_base_url}/api/generate",
            json={
                "model": target_name,
                "prompt": "ready?",
                "stream": False,
                "keep_alive": "30m",
                "options": {"num_predict": 4},
            },
        ) as resp:
            if resp.status != 200:
                typer.echo(f"  warm-up HTTP {resp.status} — skipping")
            else:
                await resp.json()
        typer.echo("  ok. Model is resident.")

    asyncio.run(_setup())


@app.command()
def watch(
    room: str = typer.Argument(..., help="Project room ID to watch (e.g. !abc:agora.local)"),
) -> None:
    """Attach to a Matrix room and print formatted events as they arrive.

    Useful for smoke-testing the observer layer without a full MCP session.
    """
    from agora.matrix.client import AgoraMatrixClient
    from agora.matrix.sync import EventDispatcher
    from agora.observe import formatters
    from agora.observe.sync_service import SyncService

    settings = get_settings()

    async def _main() -> None:
        client = AgoraMatrixClient(
            homeserver=settings.matrix_homeserver, user_id=settings.matrix_user_id
        )
        if settings.matrix_password:
            await client.login(settings.matrix_password)

        dispatcher = EventDispatcher()

        async def _print_phase(_room, change):
            msg = formatters.format_phase_change(change)
            typer.echo(msg.body)

        async def _print_task(_room, parsed):
            msg = formatters.format_task_started(parsed)
            typer.echo(msg.body)

        async def _print_result(_room, parsed):
            msg = formatters.format_task_completed(parsed)
            typer.echo(msg.body)

        async def _print_learning(_room, learning):
            msg = formatters.format_learning(learning)
            typer.echo(msg.body)

        dispatcher.on_phase_change(_print_phase)
        dispatcher.on_task_event(_print_task)
        dispatcher.on_task_result(_print_result)
        dispatcher.on_learning(_print_learning)

        service = SyncService(client, dispatcher, rooms=[room])
        typer.echo(f"watching {room} (Ctrl-C to stop)")
        try:
            await service.run()
        finally:
            await client.close()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


@app.command()
def doctor() -> None:
    """Report which LLM backends are available on this machine."""
    import shutil

    import aiohttp

    from agora.fleet.vram import probe_free_vram_mib

    settings = get_settings()

    async def _probe() -> None:
        typer.echo("== agora doctor ==")

        # Ollama
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3, sock_connect=2)
            ) as s, s.get(f"{settings.ollama_base_url}/api/tags") as resp:
                typer.echo(f"ollama  : OK (HTTP {resp.status}) at {settings.ollama_base_url}")
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"ollama  : unreachable ({type(exc).__name__})")

        # claude CLI
        claude_path = shutil.which(settings.claude_code_binary)
        if claude_path:
            typer.echo(f"claude  : found at {claude_path}")
            typer.echo(
                f"          subprocess enabled: {settings.allow_claude_subprocess}"
            )
        else:
            typer.echo("claude  : not on PATH")

        # Anthropic API key
        typer.echo(f"api-key : {'set' if settings.anthropic_api_key else 'unset'}")

        # VRAM
        free = await probe_free_vram_mib()
        if free is None:
            typer.echo("vram    : probe unavailable (nvidia-smi / rocm-smi not found)")
        else:
            rec = _recommend_model(free)
            typer.echo(f"vram    : {free} MiB free → recommended model: {rec}")

    asyncio.run(_probe())


def _recommend_model(free_mib: int) -> str:
    if free_mib >= 24_000:
        return "ollama/qwen2.5-coder:32b"
    if free_mib >= 10_000:
        return "ollama/qwen2.5-coder:14b"
    if free_mib >= 6_000:
        return "ollama/qwen2.5:7b-instruct"
    if free_mib >= 4_000:
        return "ollama/qwen2.5:3b-instruct"
    return "claude-code/subscription (VRAM too low for local models)"


def _build_adapter(model: str, settings):
    """Route a model string to the right adapter with settings-aware kwargs."""
    from agora.fleet.llm_adapter import create_llm_adapter

    if model.startswith("ollama/"):
        return create_llm_adapter(
            model,
            base_url=settings.ollama_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if model.startswith("claude-code/"):
        return create_llm_adapter(
            model,
            binary=settings.claude_code_binary,
            allow=settings.allow_claude_subprocess,
            timeout_seconds=settings.claude_code_timeout_seconds,
        )
    return create_llm_adapter(
        model,
        api_key=settings.anthropic_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )


if __name__ == "__main__":
    app()
