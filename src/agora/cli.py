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


@app.command()
def bench(
    profile: str = typer.Argument(
        ..., help="profiles.yaml profile to benchmark (its model is the one measured)."
    ),
    battery: str = typer.Option(
        "benchmarks/standard-v1.yaml", "--battery", help="Battery YAML to run."
    ),
    output_dir: str = typer.Option(
        "", "--output-dir", help="Run output dir (default runs_out/bench/<battery>-<profile>)."
    ),
    matrix: str = typer.Option(
        "capability-matrix.csv", "--matrix", help="Canonical CSV matrix to append to."
    ),
    run: bool = typer.Option(
        True, "--run/--no-run", help="--no-run generates the campaign and stops (no live run)."
    ),
) -> None:
    """Benchmark a profile through a battery and append keyed rows to the matrix.

    One command: generate a campaign from the battery, run it through the existing
    campaign harness, capture the model's manifest digest, then derive + append
    the keyed capability-vector rows. ``--no-run`` stops after generating the
    campaign (offline).
    """
    import sys
    from pathlib import Path

    import yaml

    from agora.bench.battery import battery_to_campaign, load_battery
    from agora.fleet.profiles import load_profiles

    settings = get_settings()
    bat = load_battery(battery)
    prof = load_profiles().select(profile)

    out = Path(output_dir or f"runs_out/bench/{bat.battery_version}-{profile}")
    out.mkdir(parents=True, exist_ok=True)
    campaign = battery_to_campaign(bat, profile, str(out))
    campaign_path = out / "campaign.yaml"
    campaign_path.write_text(yaml.safe_dump(campaign, sort_keys=False), encoding="utf-8")
    typer.echo(f"battery {bat.battery_version}  profile {profile}  model {prof.model}")
    typer.echo(f"campaign -> {campaign_path}  ({len(campaign['runs'])} runs)")

    if not run:
        typer.echo(f"--no-run: launch it with  python scripts/run_campaign.py {campaign_path}")
        return

    from agora.observe.jsonl import query_ollama_digest

    digest = query_ollama_digest(prof.model, settings.ollama_base_url)
    if digest == "unknown":
        typer.echo(
            f"could not read a manifest digest for {prof.model} from Ollama at "
            f"{settings.ollama_base_url} — is it pulled and the daemon up?"
        )
        raise typer.Exit(code=1)
    typer.echo(f"digest {digest}")

    # Live run via the existing campaign harness (repo checkout only).
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from scripts.run_campaign import run_campaign
    except ImportError as exc:
        typer.echo("agora bench must be run from the repo checkout (scripts/ not importable).")
        raise typer.Exit(code=1) from exc
    run_campaign(str(campaign_path))

    from agora.bench.ingest import ingest_run_dir
    from agora.bench.matrix import append_rows

    rows = ingest_run_dir(out, model_digest=digest, battery_version=bat.battery_version)
    combined = append_rows(matrix, rows)
    typer.echo(f"appended {len(rows)} rows -> {matrix}  ({len(combined)} total)")


@app.command()
def contribute(
    output_dir: str = typer.Argument(..., help="A completed bench run directory."),
    digest: str = typer.Option(..., "--digest", help="Model manifest digest (sha256:...)."),
    battery: str = typer.Option("standard-v1", "--battery", help="battery_version this run measured."),
    contributor: str = typer.Option("", "--contributor", help="Your GitHub user or a label."),
    dest: str = typer.Option("dist/exchange", "--dest", help="Where to write the submission."),
    gpu: str = typer.Option("", "--gpu", help="Attestation: GPU model."),
    os_name: str = typer.Option("", "--os", help="Attestation: OS."),
    write: bool = typer.Option(False, "--write", help="Write it (default: dry-run only)."),
) -> None:
    """Package a bench run into an exchange submission (dry-run by default).

    Derives the manifest, gathers the attestation, SANITIZES machine-private
    strings (with a printed scrub report), then runs the SAME validator the
    exchange CI runs — failing early, locally. ``--write`` materializes the files.
    """
    from agora.exchange.package import package_submission
    from agora.exchange.sanitize import scrub_report_lines

    extra = {k: v for k, v in {"gpu": gpu, "os": os_name}.items() if v}
    result = package_submission(
        output_dir, model_digest=digest, battery_version=battery, contributor=contributor,
        dest=dest, attestation_extra=extra, dry_run=not write,
    )
    typer.echo(f"submission: {result.submission_dir}")
    typer.echo(
        f"rows {len(result.manifest.rows)}  battery {result.manifest.battery_version}  "
        f"probe {result.manifest.probe_version}"
    )
    for line in scrub_report_lines(result.scrub_report):
        typer.echo(f"  scrub: {line}")
    if result.problems:
        typer.echo(f"INVALID ({len(result.problems)} problem(s)):")
        for p in result.problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(code=1)
    typer.echo(
        "written. review it, then open a PR against the exchange repo."
        if result.written
        else "dry-run OK. re-run with --write to materialize the submission."
    )


cast_app = typer.Typer(help="Cast (role→profile binding) commands")
app.add_typer(cast_app, name="cast")


@cast_app.command("validate")
def cast_validate(
    cast_path: str = typer.Argument(..., help="Path to a casts/<envelope>.yaml file."),
    profiles_path: str = typer.Option(None, "--profiles", help="Override profiles.yaml path."),
    matrix: str = typer.Option(
        "", "--matrix", help="Capability matrix CSV — verifies any matrix-row evidence citations."
    ),
    roles_path: str = typer.Option(
        "", "--roles", help="roles.yaml — required with --matrix to check citations against role requirements."
    ),
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

    matrix_df = roles_set = None
    if matrix:
        from agora.bench.matrix import load_matrix
        from agora.fleet.roles import load_roles

        matrix_df = load_matrix(matrix)
        roles_set = load_roles(roles_path or "roles.yaml")

    # Residency sizes from the local Ollama manifest store (best-effort; the
    # size query has its own heuristic fallback when the daemon is unreachable).
    sizes = asyncio.run(ollama_sizes_gb(cast, profiles, settings.ollama_base_url))
    errors = validate_cast(cast, profiles, sizes_gb=sizes, matrix=matrix_df, roles=roles_set)
    if errors:
        typer.echo(f"INVALID cast {cast.name!r} ({len(errors)} problem(s)):")
        for e in errors:
            typer.echo(f"  - {e}")
        raise typer.Exit(code=1)
    typer.echo(f"OK: cast {cast.name!r} valid ({len(cast.bindings)} bindings).")


@cast_app.command("eligible")
def cast_eligible(
    role: str = typer.Argument(..., help="Role name from roles.yaml."),
    matrix: str = typer.Option("capability-matrix.csv", "--matrix", help="Capability matrix CSV."),
    roles_path: str = typer.Option("roles.yaml", "--roles", help="roles.yaml path."),
    probe_version: int = typer.Option(None, "--probe-version", help="Restrict to one probe version."),
) -> None:
    """List models eligible for a role — those with a passing measurement at the
    role's harness key in the capability matrix.
    """
    from agora.bench.eligibility import evaluate_role
    from agora.bench.matrix import load_matrix
    from agora.core.errors import AgoraError
    from agora.fleet.roles import load_roles

    try:
        role_obj = load_roles(roles_path).role(role)
    except AgoraError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    if role_obj.measured is None:
        typer.echo(
            f"role {role!r} requires {role_obj.requires!r} - not matrix-measured; "
            f"cast it with a waiver or a human binding."
        )
        raise typer.Exit(code=0)

    results = evaluate_role(load_matrix(matrix), role_obj, probe_version=probe_version)
    passing = [r for r in results if r.eligible]
    if not passing:
        typer.echo(f"No models eligible for {role!r} in {matrix} (at the role's harness key).")
        for r in results:
            typer.echo(f"  [--] {r.model} ({r.model_digest[:19]}): {'; '.join(r.failures)}")
        raise typer.Exit(code=0)

    typer.echo(f"Eligible for {role!r} ({len(passing)}):")
    for r in sorted(passing, key=lambda x: x.model):
        typer.echo(f"  [OK] {r.model}  digest={r.model_digest[:19]}  measured={r.date}")


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


exchange_app = typer.Typer(help="Capability exchange commands")
app.add_typer(exchange_app, name="exchange")


@exchange_app.command("validate")
def exchange_validate(
    submission: str = typer.Argument(..., help="A packaged submission directory (with manifest.yaml)."),
) -> None:
    """Validate a packaged submission — the exact gate the exchange CI runs.

    Loads the manifest + attestation + gzipped records and re-derives the vector
    from the records; exits non-zero on any problem.
    """
    from agora.exchange.index import load_submission_records
    from agora.exchange.validate import validate_submission

    manifest, attestation, runs, tasks = load_submission_records(submission)
    problems = validate_submission(manifest, attestation, runs, tasks)
    if problems:
        typer.echo(f"INVALID ({len(problems)} problem(s)):")
        for p in problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(code=1)
    typer.echo(f"OK: {len(manifest.rows)} rows re-derive from the records; attestation consistent.")


@exchange_app.command("index")
def exchange_index(
    contributions: str = typer.Argument(..., help="A tree of submission dirs (each with manifest.yaml)."),
    out: str = typer.Option(".", "--out", help="Where to write index/matrix.csv + index/conflicts.md."),
) -> None:
    """Build the derived index (matrix.csv + conflicts.md) from all submissions.

    The same aggregation the exchange CI runs — reproduction counts across
    agreeing submissions, conflicts surfaced (never averaged).
    """
    from pathlib import Path

    from agora.exchange.index import build_index, write_index

    dirs = sorted(p.parent for p in Path(contributions).rglob("manifest.yaml"))
    if not dirs:
        typer.echo(f"no submissions (manifest.yaml) found under {contributions}")
        raise typer.Exit(code=1)
    result = build_index(dirs)  # type: ignore[arg-type]
    index_dir = write_index(out, result)
    typer.echo(
        f"index -> {index_dir}  ({len(result.matrix)} rows, {len(result.conflicts)} conflict(s))"
    )


if __name__ == "__main__":
    app()
