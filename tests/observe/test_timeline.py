from agora.observe.timeline import build_timeline


def _event(etype: str, content: dict, event_id: str = "$e1", ts: str = "2026-04-15T00:00:00+00:00") -> dict:
    c = dict(content)
    c.setdefault("timestamp", ts)
    return {"type": etype, "event_id": event_id, "sender": "@a:agora.local", "content": c}


def test_build_timeline_orders_by_timestamp() -> None:
    events = [
        ("!r1", _event("m.agora.phase_change", {"from_phase": "init", "to_phase": "analysis", "reason": "go"}, "$b", "2026-04-15T00:00:02+00:00")),
        ("!r1", _event("m.agora.phase_change", {"from_phase": "analysis", "to_phase": "architecture", "reason": "next"}, "$a", "2026-04-15T00:00:01+00:00")),
    ]
    entries = build_timeline(events)
    assert len(entries) == 2
    assert entries[0].timestamp.endswith(":01+00:00")
    assert entries[1].timestamp.endswith(":02+00:00")


def test_build_timeline_filters_non_agora_by_default() -> None:
    events = [
        ("!r1", _event("m.room.message", {"body": "hi"}, "$m")),
        ("!r1", _event("m.agora.learning", {"id": "l", "category": "pattern", "content": "x", "confidence": 0.5, "task_ref": "t"}, "$l")),
    ]
    entries = build_timeline(events)
    assert len(entries) == 1
    assert entries[0].category == "learning"


def test_build_timeline_include_non_agora() -> None:
    events = [
        ("!r1", _event("m.room.message", {"body": "hi"}, "$m")),
    ]
    entries = build_timeline(events, include_non_agora=True)
    assert len(entries) == 1
    assert entries[0].category == "other"


def test_summary_per_category() -> None:
    events = [
        ("!r", _event("m.agora.task", {"task_id": "abc12345", "status": "pending", "description": "do X"}, "$t")),
        ("!r", _event("m.agora.task_result", {"task_id": "abc12345", "success": True, "artifacts": ["a.py"]}, "$tr")),
        (
            "!r",
            _event(
                "m.agora.learning",
                {"id": "l", "category": "pattern", "content": "use DI", "confidence": 0.8, "task_ref": "t"},
                "$l",
            ),
        ),
    ]
    entries = build_timeline(events)
    summaries = [e.summary for e in entries]
    assert any("task abc12345" in s for s in summaries)
    assert any("abc12345" in s and "1 artifact" in s for s in summaries)
    assert any("learning [pattern]" in s and "use DI" in s for s in summaries)
