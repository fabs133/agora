from pathlib import Path

from agora.observe.export import ReportContext, render_report, write_report


def _ctx() -> ReportContext:
    return ReportContext(
        project_name="demo",
        project_id="proj-1",
        phase="done",
        started_at="2026-04-15T00:00:00+00:00",
        ended_at="2026-04-15T00:10:00+00:00",
        total_tokens={"input_tokens": 1234, "output_tokens": 567},
        duration_seconds=600.0,
        agents=["architect", "impl"],
    )


def _events() -> list[tuple[str, dict]]:
    return [
        (
            "!room:agora.local",
            {
                "type": "m.agora.phase_change",
                "event_id": "$p1",
                "content": {
                    "from_phase": "init",
                    "to_phase": "analysis",
                    "reason": "kicking off",
                    "timestamp": "2026-04-15T00:00:01+00:00",
                },
            },
        ),
        (
            "!room:agora.local",
            {
                "type": "m.agora.task",
                "event_id": "$t1",
                "content": {
                    "task_id": "t1",
                    "description": "plan",
                    "status": "done",
                    "agent_id": "architect",
                    "fingerprint": "",
                    "timestamp": "2026-04-15T00:00:02+00:00",
                },
            },
        ),
        (
            "!room:agora.local",
            {
                "type": "m.agora.task_result",
                "event_id": "$t1r",
                "content": {
                    "task_id": "t1",
                    "success": True,
                    "artifacts": ["plan.md"],
                    "postcondition_results": [],
                    "timestamp": "2026-04-15T00:00:03+00:00",
                },
            },
        ),
    ]


def test_render_report_includes_expected_sections() -> None:
    html = render_report(_ctx(), _events())
    assert "<!DOCTYPE html>" in html
    assert "demo" in html
    assert "Kanban" in html
    assert "Project journey" in html
    assert "plan" in html


def test_render_report_with_learning_tally() -> None:
    html = render_report(
        _ctx(),
        _events(),
        learnings_by_agent={
            "architect": [
                {
                    "category": "pattern",
                    "content": "prefer DI",
                    "confidence": 0.85,
                    "reinforcement_count": 3,
                }
            ]
        },
    )
    assert "prefer DI" in html
    assert "Learnings" in html
    assert "85%" in html  # confidence bar label


def test_write_report_creates_file(tmp_path: Path) -> None:
    out = tmp_path / "sub" / "report.html"
    path = write_report(out, _ctx(), _events())
    assert out.is_file()
    assert path == str(out)
    content = out.read_text(encoding="utf-8")
    assert "<html" in content.lower()
