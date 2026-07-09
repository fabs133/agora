"""Auto-vote on plan-builder decisions in a fixed order.

Logs in as the observer user (from Settings), finds the plan-builder
project room by substring, and posts ``/agora decision <answer>`` for each
decision as it becomes pending. Used to unblock live runs without human
clicks when the answers are already known.

Usage:
    python scripts/auto_vote_plan_builder.py \
        --output-file <path-to-plan-builder-run-stdout> \
        --answers approve stdlib-only in-memory

``--output-file`` points at the stdout capture of ``run_plan_builder.py``.
The voter tails it for ``stage 1/1 task=<X> name=<Y> kind=decision`` events
and posts the next answer from the list after each such event it hasn't
seen yet. Sends the next answer ~3s after the stage dispatch so the poll
event has time to land in the room.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

from nio import AsyncClient, LoginResponse, RoomSendResponse

# Repo-relative import so this standalone helper can read the one config source.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agora.config import get_settings  # noqa: E402

STAGE_RE = re.compile(
    r"stage 1/1 task=(?P<task>\w+) name=(?P<stage>\w+) kind=decision"
)
SYNC_ROOMS_RE = re.compile(r"sync_service: starting loop \(rooms=\[(?P<rooms>[^\]]+)\]")


def project_room_from_log(log_path: Path) -> str | None:
    """Parse the planner's ``sync_service: starting loop (rooms=[...])`` line
    and return the FIRST room id. The observer's sync service is started
    with the project room first, so index 0 is the authoritative target for
    ``/agora decision`` commands. Returns None if the log hasn't produced
    that line yet."""
    if not log_path.exists():
        return None
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = SYNC_ROOMS_RE.search(line)
                if m:
                    raw = m.group("rooms")
                    ids = [chunk.strip().strip("'\"") for chunk in raw.split(",")]
                    ids = [x for x in ids if x.startswith("!")]
                    if ids:
                        return ids[0]
    except OSError:
        return None
    return None


async def wait_for_project_room(log_path: Path, timeout: float = 60.0) -> str:
    """Poll the planner log until the sync_service announces its rooms."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rid = project_room_from_log(log_path)
        if rid:
            return rid
        await asyncio.sleep(0.5)
    raise RuntimeError(
        f"timed out waiting for 'sync_service: starting loop (rooms=...)' in {log_path}"
    )


async def send_command(client: AsyncClient, room_id: str, body: str) -> None:
    resp = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": body},
    )
    if not isinstance(resp, RoomSendResponse):
        raise RuntimeError(f"room_send failed: {resp!r}")
    print(f"[voter] posted: {body}")


async def tail_and_vote(
    log_path: Path,
    client: AsyncClient,
    room_id: str,
    answers: list[str],
    settle_seconds: float,
) -> None:
    """Tail ``log_path`` for decision-stage dispatch events. For each new
    event, post the next answer from ``answers`` after a short delay to
    let the poll event itself land first."""
    seen: set[tuple[str, str]] = set()
    queue: list[str] = list(answers)
    # Read existing contents first (the decision may already have posted
    # before this script started) so we don't miss the brief-approval stage.
    if log_path.exists():
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = STAGE_RE.search(line)
                if m and queue:
                    key = (m.group("task"), m.group("stage"))
                    if key not in seen:
                        seen.add(key)
                        answer = queue.pop(0)
                        print(f"[voter] catching up on {key}, sending {answer!r}")
                        await asyncio.sleep(settle_seconds)
                        await send_command(
                            client, room_id, f"/agora decision {answer}"
                        )
    # Now live-tail for remaining decisions.
    offset = log_path.stat().st_size if log_path.exists() else 0
    while queue:
        if not log_path.exists():
            await asyncio.sleep(0.5)
            continue
        size = log_path.stat().st_size
        if size < offset:
            # Log rotated — start from beginning.
            offset = 0
        if size == offset:
            await asyncio.sleep(0.5)
            continue
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            chunk = fh.read()
            offset = fh.tell()
        for line in chunk.splitlines():
            m = STAGE_RE.search(line)
            if not m:
                continue
            key = (m.group("task"), m.group("stage"))
            if key in seen:
                continue
            seen.add(key)
            if not queue:
                print(f"[voter] saw {key} but no more answers queued")
                break
            answer = queue.pop(0)
            print(f"[voter] detected {key}, sending {answer!r}")
            await asyncio.sleep(settle_seconds)
            await send_command(client, room_id, f"/agora decision {answer}")
    print("[voter] all answers posted")


async def main_async() -> int:
    # Defaults come from the one config source (Settings); env is read only in
    # config.py. --homeserver/--user/--password still override on the CLI.
    _settings = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-file", required=True, type=Path)
    ap.add_argument("--answers", nargs="+", required=True)
    ap.add_argument("--homeserver", default=_settings.matrix_homeserver)
    ap.add_argument("--user", default=_settings.observer_user)
    ap.add_argument("--password", default=_settings.observer_password)
    ap.add_argument(
        "--room-id",
        default=None,
        help="Explicit project room id. If omitted, parsed from the log's "
        "'sync_service: starting loop (rooms=[...])' line.",
    )
    ap.add_argument("--settle-seconds", type=float, default=3.0)
    args = ap.parse_args()

    if args.room_id:
        room_id = args.room_id
    else:
        print("[voter] waiting for project room id from planner log …")
        room_id = await wait_for_project_room(args.output_file, timeout=120.0)
    print(f"[voter] targeting room {room_id}")

    client = AsyncClient(args.homeserver, args.user)
    try:
        resp = await client.login(password=args.password, device_name="agora-voter")
        if not isinstance(resp, LoginResponse):
            print(f"[voter] login failed: {resp!r}", file=sys.stderr)
            return 1
        print(f"[voter] logged in as {args.user}")
        # One sync so nio has a usable state before room_send.
        await client.sync(timeout=5000)
        # Accept the observer invite if we haven't joined yet — freshly-created
        # project rooms require an explicit join before room_send is authorized.
        # nio's AsyncClient.join sends a POST without body, which Conduit
        # rejects as M_BAD_JSON — use a raw authenticated POST with ``{}``.
        joined = await client.joined_rooms()
        if room_id not in joined.rooms:
            from urllib.parse import quote as url_quote

            import aiohttp

            url = (
                f"{args.homeserver}/_matrix/client/v3/join/{url_quote(room_id)}"
            )
            async with aiohttp.ClientSession() as sess, sess.post(
                url,
                json={},
                headers={"Authorization": f"Bearer {client.access_token}"},
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(
                        f"room_join failed ({resp.status}): {body}"
                    )
                print(f"[voter] joined {room_id} (raw POST)")
            await client.sync(timeout=5000)
        await tail_and_vote(
            args.output_file, client, room_id, list(args.answers), args.settle_seconds
        )
        return 0
    except Exception as exc:
        print(f"[voter] error: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
