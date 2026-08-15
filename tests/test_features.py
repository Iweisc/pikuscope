"""Tests for chat command parsing, learnings store, and rendering."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pikuscope.commands import parse_command
from pikuscope.learnings import LearningsStore
from pikuscope.render import render_finding_comment, render_summary_comment, MARKER
from pikuscope.review import Finding, ReviewResult


def test_parse_command_basic():
    assert parse_command("@pikuscope review") == ("review", "")
    assert parse_command("@pikuscope full review") == ("full review", "")
    assert parse_command("@pikuscope generate docstrings") == ("generate docstrings", "")
    assert parse_command("@pikuscope generate unit tests") == ("generate unit tests", "")
    assert parse_command("@pikuscope autofix") == ("autofix", "")
    assert parse_command("please @pikuscope pause") == ("pause", "")


def test_parse_command_remember_and_chat():
    cmd, args = parse_command("@pikuscope remember never flag SVG icon duplication")
    assert cmd == "remember"
    assert args == "never flag SVG icon duplication"
    cmd, args = parse_command("@pikuscope why did the cache key change?")
    assert cmd == "chat"
    assert "cache key" in args


def test_parse_command_ignores_unaddressed():
    assert parse_command("this is just a normal comment") is None
    assert parse_command("@someoneelse review") is None


def test_parse_command_case_insensitive():
    assert parse_command("@PikuScope Review")[0] == "review"


def test_learnings_scope(tmp_path):
    store = LearningsStore(tmp_path / "learn.jsonl")
    store.add("global rule")
    store.add("mobile only rule", scope="apps/mobile/**")
    assert store.for_paths(["apps/web/src/a.ts"]) == ["global rule"]
    got = store.for_paths(["apps/mobile/src/b.ts"])
    assert got == ["global rule", "mobile only rule"]


def test_render_finding_suggestion_and_agent_prompt():
    f = Finding(path="a.ts", start_line=3, end_line=4, severity="major", category="bug",
                title="Fix it", body="Broken.", suggestion="const x = 1;",
                failure_scenario="x is undefined on load")
    md = render_finding_comment(f)
    assert "```suggestion" in md and "const x = 1;" in md
    assert "Prompt for AI agents" in md and "a.ts:3-4" in md
    md2 = render_finding_comment(f, ai_prompt=False)
    assert "Prompt for AI agents" not in md2


def test_render_summary_marker_and_walkthrough():
    r = ReviewResult(summary="- did things", walkthrough=[{"files": "a.ts", "summary": "changed | stuff"}])
    md = render_summary_comment(r)
    assert MARKER in md
    assert "changed \\| stuff" in md  # pipe escaping inside table
