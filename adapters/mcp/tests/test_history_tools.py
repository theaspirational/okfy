"""What an agent learns after E_WRITE_BASE_MOVED: who moved the base, and
what changed (okfy_diff). Nothing here records anything; git is the source."""
import subprocess
from pathlib import Path

import pytest

from okfy import frontmatter
from okfy.init import init_bundle
from okfy_mcp import handlers
from okfy_mcp.resolve import Target

ACTOR_A, ACTOR_B = "claude-code/1.0", "codex/1.0"
NOTE = """---
type: note
title: Session note A
description: What the agent saw in session A.
---

First line.
"""
PLAN = """---
type: ExtractionPlan
title: Test plan
archetype: decision-support
archetype_version: 1
types:
  note: session notes
segments: []
---

Plan.
"""


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "okfy test")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "test@example.invalid")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("# A\n", encoding="utf-8")
    root = init_bundle(tmp_path / "b", corpus)
    purpose = root / "meta/purpose.md"
    meta, body = frontmatter.parse(purpose.read_text(encoding="utf-8"))
    meta["direct"] = {"types": ["note"]}
    purpose.write_text(frontmatter.serialize(meta, body), encoding="utf-8")
    (root / "meta/extraction-plan.md").write_text(PLAN, encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes/a.md").write_text(NOTE, encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-q", "-m", "fixture")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _write(t, actor, sha, new, old="First line."):
    return handlers.h_write(t, "notes/a", actor=actor, expect_sha256=sha,
                            patch=[{"old": old, "new": new}])


def test_base_moved_names_the_mover(root):
    t = Target(root)
    sha0 = handlers.h_show(t, concept_id="notes/a")["sha256"]
    won = _write(t, ACTOR_A, sha0, "Agent one.")
    assert won["channel"] == "direct"
    lost = _write(t, ACTOR_B, sha0, "Agent two.")
    assert lost["error"] == "E_WRITE_BASE_MOVED"
    assert lost["current_sha256"] == won["sha256"]
    assert lost["changed_by"] == ACTOR_A
    assert lost["changed_at"] and len(lost["commit"]) == 40


def test_diff_from_the_version_the_agent_read(root):
    t = Target(root)
    sha0 = handlers.h_show(t, concept_id="notes/a")["sha256"]
    won = _write(t, ACTOR_A, sha0, "Agent one.")
    d = handlers.h_diff(t, "notes/a", sha0)
    assert d["sha256"] == won["sha256"] and d["changed_by"] == ACTOR_A
    assert "-First line." in d["diff"] and "+Agent one." in d["diff"]
    assert len(d["base_commit"]) == 40
    same = handlers.h_diff(t, "notes/a", won["sha256"])
    assert same["unchanged"] is True and same["diff"] == ""
    unknown = handlers.h_diff(t, "notes/a", "0" * 64)
    assert unknown["error"] == "E_DIFF_BASE_UNKNOWN" and unknown["sha256"] == won["sha256"]


def test_multi_show_carries_the_frontmatter(root):
    out = handlers.h_show(Target(root), concept_ids=["notes/a"])
    (c,) = out["concepts"]
    assert "title: Session note A" in c["frontmatter"]
    assert c["body"].startswith("\nFirst line.") or c["body"].startswith("First line.")


def test_history_is_the_ledger_newest_first(root):
    t = Target(root)
    sha0 = handlers.h_show(t, concept_id="notes/a")["sha256"]
    first = _write(t, ACTOR_A, sha0, "Agent one.")
    second = _write(t, ACTOR_B, first["sha256"], "Agent two.", old="Agent one.")
    assert second["channel"] == "direct"
    h = handlers.h_history(t, "notes/a")
    assert h["count"] == 2 and h["problems"] == []
    assert [e["actor"] for e in h["events"]] == [ACTOR_B, ACTOR_A]
    assert {e["event"] for e in h["events"]} == {"accept"}
    assert {e["origin"] for e in h["events"]} == {"agent-direct"}
    assert h["events"][0]["at"] >= h["events"][1]["at"]
    assert len(handlers.h_history(t, "notes/a", limit=1)["events"]) == 1
    assert handlers.h_history(t, "notes/a", proposal="proposals/none")["events"] == []
