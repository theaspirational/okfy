"""`okfy_write`: one tool, two lanes. The key sets below are the response
contract — additive only, like every other tool's (adapters/mcp/README.md)."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from okfy import frontmatter, memory
from okfy.bundle import Bundle
from okfy.init import init_bundle
from okfy.proposals import list_proposals
from okfy.workspace import init_workspace
from okfy_mcp import handlers
from okfy_mcp.resolve import Target
from okfy_mcp.server import build_server
from okfy_mcp.session import Session

ACTOR = "claude-code/1.0"
NOTE_A = """---
type: note
title: Session note A
description: What the agent saw in session A.
---

First line.
"""
DECISION = """---
type: decision
title: Decision one
description: An owner-reviewed decision.
---

We decided.
"""
PLAN = """---
type: ExtractionPlan
title: Test plan
archetype: decision-support
archetype_version: 1
types:
  note: session notes
  decision: reviewed decisions
segments: []
---

Plan.
"""
DIRECT_KEYS = {"persisted", "channel", "id", "action", "sha256", "origin",
               "proposal", "path"}
PROPOSAL_KEYS = {"persisted", "channel", "proposal", "path"}
REFUSAL_KEYS = {"error", "message", "way_out"}


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
    for rel, text in (("notes/a.md", NOTE_A), ("decisions/d1.md", DECISION)):
        (root / rel).parent.mkdir(exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-q", "-m", "fixture")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def with_body(text: str, body: str) -> str:
    return text.rsplit("---\n", 1)[0] + "---\n" + body


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.parts and ".okfy-cache" not in p.parts}


# 13
def test_direct_lane_is_live_and_the_sha_matches_a_fresh_show(root):
    t, session = Target(root), Session()
    shown = handlers.h_show(t, concept_id="notes/a", session=session)
    out = handlers.h_write(t, "notes/a", ACTOR, shown["sha256"],
                           content=with_body(shown["content"], "\nLive now.\n"),
                           session=session)
    assert set(out) == DIRECT_KEYS
    assert (out["channel"], out["persisted"], out["proposal"], out["path"]) == (
        "direct", True, None, "notes/a.md")
    again = handlers.h_show(t, concept_id="notes/a")
    assert again["sha256"] == out["sha256"] and "Live now." in again["content"]
    assert handlers.h_fresh(t, {"notes/a": shown["sha256"]}) == {"notes/a": "changed"}
    # what this process saw, on the ledger row — and only its counters
    observed = memory.events(Bundle(root))[0][-1]["observed"]
    assert observed["target_shown"] is True and observed["base_shown"] is True
    assert "read_set" not in observed


def test_a_write_against_a_sha_this_session_never_showed_says_so(root):
    t = Target(root)
    sha = handlers.h_show(t, concept_id="notes/a")["sha256"]     # another session
    handlers.h_write(t, "notes/a", ACTOR, sha, session=Session(),
                     patch=[{"old": "First line.", "new": "Blind."}])
    observed = memory.events(Bundle(root))[0][-1]["observed"]
    assert observed["target_shown"] is False and observed["base_shown"] is False


# 14
def test_other_types_fall_back_to_a_proposal(root):
    t = Target(root)
    shown = handlers.h_show(t, concept_id="decisions/d1")
    out = handlers.h_write(t, "decisions/d1", ACTOR, shown["sha256"],
                           content=with_body(DECISION, "\nChanged our mind.\n"),
                           note="new evidence")
    assert set(out) >= PROPOSAL_KEYS and "sha256" not in out
    assert (out["channel"], out["persisted"]) == ("proposal", True)
    assert (root / out["path"]).is_file() and out["path"].startswith("proposals/")
    assert (root / "decisions/d1.md").read_text(encoding="utf-8") == DECISION
    rows = list_proposals(Bundle(root))
    assert [r["target"] for r in rows] == ["decisions/d1"]
    env = Bundle(root).get(out["proposal"]).meta
    assert env["proposal"]["base_sha256"] == shown["sha256"]
    assert env["evidence"] == {"kind": "agent-inference"}


def test_fallback_takes_the_whole_text_of_a_verified_concept(root):
    """okfy_write tells the agent to send owner fields back as okfy_show
    returned them. On a concept an accept already verified, that text carries
    `verified` — which must not turn the fallback into a refusal, while a
    verification the agent made up still is one."""
    from okfy import proposals
    bundle, t = Bundle(root), Target(root)
    meta, body = frontmatter.parse(with_body(DECISION, "\nAccepted once.\n"))
    first = proposals.propose(bundle, meta, body, target="decisions/d1", actor=ACTOR)
    proposals.accept(bundle, bundle.concept_id(first))
    shown = handlers.h_show(t, concept_id="decisions/d1")
    assert "verified:" in shown["content"]
    out = handlers.h_write(t, "decisions/d1", ACTOR, shown["sha256"],
                           content=shown["content"].replace("Accepted once.", "Edited."))
    assert (out.get("channel"), out.get("persisted")) == ("proposal", True), out
    assert "verified" not in Bundle(root).get(out["proposal"]).meta
    forged = shown["content"].replace("by: human:", "by: human:someone-else-")
    assert forged != shown["content"]
    out = handlers.h_write(t, "decisions/d1", ACTOR, shown["sha256"], content=forged,
                           note="second")
    assert out["error"] == "E_PROPOSAL_EVIDENCE"


def test_an_unknown_archetype_is_a_refusal_and_does_not_block_routing(root):
    plan = root / "meta/extraction-plan.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace(
        "archetype: decision-support", "archetype: no-such-archetype"), encoding="utf-8")
    t = Target(root)
    # a reviewed type never needs the schema: it is still routed to proposals/
    d = handlers.h_show(t, concept_id="decisions/d1")
    out = handlers.h_write(t, "decisions/d1", ACTOR, d["sha256"],
                           content=with_body(DECISION, "\nRouted.\n"))
    assert out["channel"] == "proposal"
    # a direct type does: the refusal is the documented envelope, nothing lands
    n = handlers.h_show(t, concept_id="notes/a")
    before = (root / "notes/a.md").read_bytes()
    out = handlers.safe_call(handlers.h_write, t, "notes/a", ACTOR, n["sha256"],
                             content=with_body(NOTE_A, "\nNo schema.\n"))
    assert set(out) == REFUSAL_KEYS and "no-such-archetype" in out["message"]
    assert out["way_out"] and (root / "notes/a.md").read_bytes() == before


def test_a_bundle_with_no_lane_proposes_everything(root):
    purpose = root / "meta/purpose.md"
    meta, body = frontmatter.parse(purpose.read_text(encoding="utf-8"))
    del meta["direct"]
    purpose.write_text(frontmatter.serialize(meta, body), encoding="utf-8")
    t = Target(root)
    out = handlers.h_write(t, "notes/new", ACTOR, "absent",
                           content=NOTE_A.replace("note A", "note new"))
    assert out["channel"] == "proposal" and not (root / "notes/new.md").exists()
    assert Bundle(root).get(out["proposal"]).meta["proposal"]["action"] == "create"


# 15
@pytest.mark.parametrize("cid, text", [("notes/a", NOTE_A), ("decisions/d1", DECISION)])
def test_base_moved_in_either_lane_persists_nothing(root, cid, text):
    t = Target(root)
    current = handlers.h_show(t, concept_id=cid)["sha256"]
    before = snapshot(root)
    out = handlers.h_write(t, cid, ACTOR, "0" * 64, content=with_body(text, "\nx\n"))
    assert set(out) == REFUSAL_KEYS | {"current_sha256"}
    assert out["error"] == "E_WRITE_BASE_MOVED" and out["current_sha256"] == current
    assert out["way_out"] and snapshot(root) == before


# 16: the three shapes above, plus an ordinary coded refusal
def test_a_refusal_is_the_documented_envelope(root):
    t = Target(root)
    sha = handlers.h_show(t, concept_id="notes/a")["sha256"]
    before = snapshot(root)
    out = handlers.h_write(t, "notes/a", ACTOR, sha,
                           content=NOTE_A.replace("type: note", "type: decision"))
    assert set(out) == REFUSAL_KEYS and out["error"] == "E_DIRECT_RETYPE"
    assert snapshot(root) == before


# 17
def test_workspace_target_is_refused(root, tmp_path):
    before = snapshot(root)
    ws = tmp_path / "ws"
    init_workspace(ws, [("b", root, "knowledge")], "WS")
    out = handlers.h_write(Target(ws), "b:notes/a", ACTOR, "absent", content=NOTE_A)
    assert set(out) == REFUSAL_KEYS and out["error"] == "E_WRITE_WORKSPACE"
    assert snapshot(root) == before


def test_the_tool_is_registered_and_two_sessions_cannot_both_win(root):
    """Through the real FastMCP layer: two servers on one bundle are two
    agent sessions. Both read the same note; one write lands, the other is
    told the base moved and `okfy_fresh` agrees."""
    async def call(server, tool, **args):
        result = await server.call_tool(tool, args)
        content = result[0] if isinstance(result, tuple) else result
        return json.loads(content[0].text)

    async def scenario():
        one, two = build_server(root), build_server(root)
        assert "okfy_write" in {t.name for t in await one.list_tools()}
        a = await call(one, "okfy_show", concept_id="notes/a")
        b = await call(two, "okfy_show", concept_id="notes/a")
        won = await call(one, "okfy_write", concept_id="notes/a", actor=ACTOR,
                         expect_sha256=a["sha256"],
                         patch=[{"old": "First line.", "new": "Agent one."}])
        lost = await call(two, "okfy_write", concept_id="notes/a", actor=ACTOR,
                          expect_sha256=b["sha256"],
                          patch=[{"old": "First line.", "new": "Agent two."}])
        fresh = await call(two, "okfy_fresh", ids={"notes/a": b["sha256"]})
        return won, lost, fresh

    won, lost, fresh = asyncio.run(scenario())
    assert won["channel"] == "direct"
    assert lost["error"] == "E_WRITE_BASE_MOVED" and lost["current_sha256"] == won["sha256"]
    assert fresh == {"notes/a": "changed"}
    assert "Agent one." in (root / "notes/a.md").read_text(encoding="utf-8")
