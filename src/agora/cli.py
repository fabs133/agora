"""Typer CLI entry point.

Subcommands:
- ``agora mcp``           — run the outer MCP server over stdio.
- ``agora watch``         — attach to a project room and stream events into the terminal.
- ``agora setup-ollama``  — VRAM-check then pull the default Ollama model.
- ``agora doctor``        — probe Ollama, GPU, and report status.
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

    from agora import doctor
    from agora.core.errors import AgoraError

    settings = get_settings()
    target = model or settings.llm_model
    if target.startswith("ollama/"):
        target_name = target.removeprefix("ollama/")
    else:
        target_name = target

    async def _setup() -> None:
        # Health checks come from agora.doctor — cli holds no private health logic.
        r = doctor.check_ollama_reachable(settings.ollama_base_url)
        typer.echo(doctor.format_line(r))
        if not r.ok:
            raise AgoraError(
                f"Cannot reach Ollama at {settings.ollama_base_url} — run `ollama serve`?"
            )

        if settings.skip_vram_check:
            typer.echo("→ VRAM check skipped (AGORA_SKIP_VRAM_CHECK=1).")
        else:
            v = await doctor.check_vram(
                target_name, settings.ollama_base_url, settings.vram_safety_margin_mib
            )
            typer.echo(doctor.format_line(v))
            if not v.ok:
                raise AgoraError(f"VRAM pre-flight failed: {v.detail}")

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
def doctor(
    cast: str = typer.Option(
        None, "--cast", help="casts/<name>.yaml — also check its models are present in Ollama."
    ),
    dev: bool = typer.Option(False, "--dev", help="Also check the PlantUML dev-tooling server."),
) -> None:
    """Preflight every external dependency (Ollama, VRAM, Conduit, workspace).

    Composition root: reads Settings and injects endpoints into agora.doctor
    (the one place health checks live). Non-zero exit on any red.
    """
    from pathlib import Path

    from agora import doctor as doc

    settings = get_settings()

    required: list[str] = []
    if cast:
        from agora.fleet.cast import load_cast, resolve_cast
        from agora.fleet.profiles import load_profiles

        profiles = load_profiles(settings.profiles_file)
        resolved = resolve_cast(load_cast(cast), profiles)
        required = [
            rb.model.removeprefix("ollama/")
            for rb in resolved
            if not rb.is_human and rb.model.startswith("ollama/") and rb.resident
        ]

    repo_root = Path(__file__).resolve().parents[2]  # src/agora/cli.py → repo root
    results = asyncio.run(
        doc.run_checks(
            ollama_base_url=settings.ollama_base_url,
            required_models=required,
            vram_model=settings.llm_model,
            vram_safety_margin_mib=settings.vram_safety_margin_mib,
            repo_root=repo_root,
            work_dir=settings.work_dir,
            homeserver=settings.matrix_homeserver,
            matrix_user_id=settings.matrix_user_id,
            matrix_password=settings.matrix_password,
            dev=dev,
            plantuml_url=settings.plantuml_url,
        )
    )
    raise typer.Exit(code=doc.report(results, echo=typer.echo))


cast_app = typer.Typer(help="Cast (role→profile binding) commands")
app.add_typer(cast_app, name="cast")


@cast_app.command("validate")
def cast_validate(
    cast_path: str = typer.Argument(..., help="Path to a casts/<envelope>.yaml file."),
    profiles_path: str = typer.Option(None, "--profiles", help="Override profiles.yaml path."),
) -> None:
    """Validate a cast against the four casting rules; exit 1 if invalid."""
    from agora.core.errors import AgoraError
    from agora.fleet.cast import load_cast, ollama_sizes_gb, validate_cast
    from agora.fleet.profiles import load_profiles

    settings = get_settings()
    try:
        profiles = load_profiles(profiles_path)
        cast = load_cast(cast_path)
    except AgoraError as exc:
        typer.echo(f"INVALID: {exc}")
        raise typer.Exit(code=1) from exc

    # Residency sizes from the local Ollama manifest store (best-effort; the
    # size query has its own heuristic fallback when the daemon is unreachable).
    sizes = asyncio.run(ollama_sizes_gb(cast, profiles, settings.ollama_base_url))
    errors = validate_cast(cast, profiles, sizes_gb=sizes)
    if errors:
        typer.echo(f"INVALID cast {cast.name!r} ({len(errors)} problem(s)):")
        for e in errors:
            typer.echo(f"  - {e}")
        raise typer.Exit(code=1)
    typer.echo(f"OK: cast {cast.name!r} valid ({len(cast.bindings)} bindings).")


@cast_app.command("load")
def cast_load(
    cast_path: str = typer.Argument(..., help="Path to a casts/<envelope>.yaml file."),
    profiles_path: str = typer.Option(None, "--profiles", help="Override profiles.yaml path."),
) -> None:
    """Resolve a cast into its role table (refuses an invalid cast)."""
    from agora.core.errors import AgoraError
    from agora.fleet.cast import load_cast, resolve_cast
    from agora.fleet.profiles import load_profiles

    try:
        profiles = load_profiles(profiles_path)
        cast = load_cast(cast_path)
        table = resolve_cast(cast, profiles)
    except AgoraError as exc:
        typer.echo(f"ERROR: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo(f"== cast {cast.name!r} on {cast.hardware.gpu} ({cast.hardware.vram_budget_gb} GB) ==")
    for rb in table:
        if rb.is_human:
            typer.echo(f"  {rb.role:20} human")
        else:
            res = "resident" if rb.resident else "on-demand"
            typer.echo(f"  {rb.role:20} {rb.model:40} {res} keep_alive={rb.keep_alive}")


def _recommend_model(free_mib: int) -> str:
    if free_mib >= 24_000:
        return "ollama/qwen2.5-coder:32b"
    if free_mib >= 10_000:
        return "ollama/qwen2.5-coder:14b"
    if free_mib >= 6_000:
        return "ollama/qwen2.5:7b-instruct"
    if free_mib >= 4_000:
        return "ollama/qwen2.5:3b-instruct"
    return "none — VRAM too low for the local models; free VRAM or pick a smaller one"


def _build_adapter(model: str, settings):
    """Route a model string to its adapter with settings-aware kwargs. Ollama is
    the only backend; other model strings are rejected by ``create_llm_adapter``."""
    from agora.fleet.llm_adapter import create_llm_adapter

    return create_llm_adapter(
        model,
        base_url=settings.ollama_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
    )


if __name__ == "__main__":
    app()
