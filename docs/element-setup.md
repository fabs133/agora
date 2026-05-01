# Observing an Agora run in Element

Element is the reference Matrix client — it's how you *see* what your agents are
doing in real time and how you *vote* during the REVIEW phase. This doc walks
through the one-time setup against your local Conduit homeserver.

## Prerequisites

- Conduit is running at `http://localhost:6167` (from `conduit/docker-compose.yml`).
- An observer user exists on Conduit. The dev seed creates `@fabs:agora.local`
  with password `fabs-dev-pass` — re-register with:

  ```bash
  curl -X POST http://localhost:6167/_matrix/client/v3/register \
    -H 'Content-Type: application/json' \
    -d '{"auth":{"type":"m.login.registration_token","token":"dev_only_CHANGE_ME"},"username":"fabs","password":"fabs-dev-pass"}'
  ```

- Verify the homeserver is reachable:

  ```bash
  curl -sS http://localhost:6167/_matrix/client/versions
  # expect: {"versions":["r0.5.0","r0.6.0","v1.1",...]}
  ```

## Install Element Desktop

Download from <https://element.io/download> and run the installer. **Prefer
Desktop over Web** for local development: Element Web over HTTP triggers
mixed-content warnings in most browsers and some features refuse to work
without HTTPS. Element Desktop talks to `http://localhost:6167` cleanly.

## Sign in

1. Launch Element.
2. Click **Sign In**.
3. Below the username field there's an **Edit** link next to the default
   homeserver. Click it.
4. Enter `http://localhost:6167` and click **Continue**.
5. Back on the sign-in screen: username `fabs`, password `fabs-dev-pass`.
6. Click **Sign In**. You should land on an empty room list.

## Joining project rooms

The orchestrator creates rooms under the `@agora:agora.local` account and
does **not** invite you automatically (that's Sprint-8 work — for now invites
are manual). You have two options.

### Option A: accept incoming invites

When an orchestrator run starts, have it invite you via one curl per room.
Grab the access token from the agora user (obtained once during registration;
see `scripts/run_discord_bot_test.py` or the registration response) and run:

```bash
AGORA_TOKEN="<token-for-@agora>"
ROOM_ID="!abc:agora.local"  # from the runner's sync_service startup log
curl -X POST \
  "http://localhost:6167/_matrix/client/v3/rooms/$ROOM_ID/invite?access_token=$AGORA_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"@fabs:agora.local"}'
```

The invite shows up in Element's left rail under **Invites** — click **Accept**.

### Option B: dev-only auto-invite on registration

A faster path during development is to patch Conduit's `conduit.toml` to add
`@fabs:agora.local` as an admin, or temporarily modify
`src/agora/matrix/room_manager.py` to include the observer in every
`create_room(invite=...)` call. Both are out of scope for this doc.

## Using the project room

Once you've joined a `project:<name>` room you can:

- **Watch the live stream**: phase banners (`🌱 init → 📝 analysis`), task cards
  (`▶ task abc12345 (architect): ...`), learnings (`💡 pattern 80% ...`).
- **Vote on the REVIEW poll**: during the REVIEW phase the orchestrator posts
  a poll with five options. Click one to decide the run's fate.
- **Send `/agora` commands** in the composer:

  | Command | Effect |
  |---------|--------|
  | `/agora note <text>` | Appends a note every agent sees at its next turn. |
  | `/agora pause` | Blocks new task dispatch at the next boundary. |
  | `/agora resume` | Releases the pause gate. |
  | `/agora abort [reason]` | Transitions the project to FAILED immediately. |
  | `/agora redirect <agent> <text>` | Overrides one agent's next-turn instructions. |
  | `/agora review <answer_id>` | Cast a poll vote without clicking (fallback). |
  | `/agora help` | Posts the command list. |

All replies land in the same room as notice-style messages.

## Troubleshooting

| Symptom | Check |
|--------|-------|
| "Cannot reach server" on sign-in | `curl http://localhost:6167/_matrix/client/versions` returns 200? Container up? `docker ps --filter name=conduit` |
| Signed in but no invites | The run hasn't reached `create_project_room` yet, or the observer wasn't invited. Check the runner's log for `sync_service: starting loop (rooms=[...])` — those are the room IDs to invite yourself into. |
| Sent `/agora pause` but nothing happened | Check the runner's log for `dispatch:` entries — pause only gates *between* tasks, not mid-LLM-call. |
| Review poll is stuck | Default `review_timeout_seconds=300`. Either vote, or wait 5 minutes for auto-review to fire. |

## Next steps

Once you've verified the basic loop, consider pulling a bigger model
(`qwen2.5-coder:32b` if VRAM allows) or wiring the Claude Code subprocess
adapter for better multi-turn reliability. See [README.md](../README.md) for
the backend-matrix.
