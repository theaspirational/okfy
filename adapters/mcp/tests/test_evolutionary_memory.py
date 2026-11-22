"""Synthetic bundle, actual registered FastMCP tools (no live endpoint)."""
import asyncio
import json
import os
import uuid

import pytest

from okfy import evolution, frontmatter, memory, proposals
from okfy.bundle import Bundle
from okfy.gitenv import run_git
from okfy.init import init_bundle
from okfy.validate import MEMORY_PROFILE, direct_policy, preedit_policy
from okfy_mcp.server import build_server

ACTOR = "fixture/1"


def git(root, *args):
    return run_git(root, *args, capture_output=True, check=True).stdout


def seed(root, cid, body):
    path = root / f"{cid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.serialize(evolution.minimal_metadata(cid, body), body))
    return path


@pytest.fixture
def root(tmp_path, monkeypatch):
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
    corpus = tmp_path / "empty"
    corpus.mkdir()
    root = init_bundle(tmp_path / "memory", corpus)
    purpose = root / "meta/purpose.md"
    meta, body = frontmatter.parse(purpose.read_text())
    meta["direct"] = {"profile": MEMORY_PROFILE}
    purpose.write_text(frontmatter.serialize(meta, body))
    seed(root, "dna", "Operator provisioned.")
    seed(root, "notes/a", "One.\n\nTwo [other](b.md).")
    seed(root, "notes/b", "Look at [one](a.md).")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "operator provisioning")
    return root


def tool(server, name, **args):
    result = asyncio.run(server.call_tool(name, args))
    return json.loads(result[0].text)


def sha(root, cid):
    return evolution.digest((root / f"{cid}.md").read_bytes())


def ident():
    return str(uuid.uuid4())


def request(action, source, destination, bases, **extra):
    return dict(actor=ACTOR, action=action, source=source, destination=destination,
                bases=bases, operation_id=ident(), **extra)


def test_write_receipt_replay_noop_and_provenance(root):
    s = build_server(root)
    cid = "core/agent-chosen/nested"
    args = dict(concept_id=cid, expect_sha256="absent", content="Grain sizes.",
                evidence={"kind": "external-source", "ref": "synthetic:session"},
                actor=ACTOR, operation_id=ident())
    result = tool(s, "okfy_write", **args)
    assert result["effect"] == "committed" and not result["replayed"]
    assert result["sha256"] == sha(root, cid)
    doc = Bundle(root).get(cid)
    assert (doc.meta["type"], doc.meta["evidence"], doc.meta["title"]) == (
        "Memory", args["evidence"], "nested")
    same = tool(s, "okfy_write", **{**args, "operation_id": ident(),
                                     "expect_sha256": result["sha256"]})
    assert same["effect"] == "no-op" and not same["persisted"]
    changed = tool(s, "okfy_write", **{**args, "operation_id": ident(), "content": "New.",
                                       "expect_sha256": result["sha256"], "actor": "other/1"})
    assert changed["effect"] == "committed"
    replay = tool(build_server(root), "okfy_write", **args)
    assert replay == {**result, "replayed": True}
    conflict = tool(s, "okfy_write", **{**args, "content": "Different."})
    assert "different request" in conflict["message"]
    history = tool(s, "okfy_history", concept_id=cid)
    assert history["count"] == 2 and history["events"][-1]["receipt"]["id"] == args["operation_id"]
    diff = tool(s, "okfy_diff", concept_id=cid, from_sha256=result["sha256"])
    assert diff["changed_by"] == "other/1" and "+New." in diff["diff"]


def test_split_move_merge_links_bases_and_history(root):
    s = build_server(root)
    a, b, child, moved = "notes/a", "notes/b", "core/deep/child", "notes/chosen"
    split = request("split", a, child, {a: sha(root, a), child: "absent"},
                    extract="Two [other](b.md).")
    v = tool(s, "okfy_reorganize", **split)
    assert v["effect"] == "committed"
    bundle = Bundle(root)
    assert bundle.get(child).body == "Two [other](/notes/b.md)."
    assert "/core/deep/child.md" in bundle.get(a).body
    uid = bundle.get(child).meta["okf_id"]
    move = request("move", child, moved, {child: sha(root, child), moved: "absent",
                                         a: sha(root, a)})
    old = git(root, "rev-parse", "HEAD")
    for args in ({**move, "bases": {**move["bases"], a: "0"*64}},
                 {**move, "bases": {child: sha(root, child), moved: "absent"}},
                 {**move, "destination": b}):
        assert "error" in tool(s, "okfy_reorganize", **args)
        assert git(root, "rev-parse", "HEAD") == old and not git(root, "status", "--porcelain")
    m = tool(s, "okfy_reorganize", **move)
    assert m["effect"] == "committed" and m["versions"][child] == "absent"
    assert bundle.get(moved).meta["okf_id"] == uid
    assert "/notes/chosen.md" in bundle.get(a).body and not bundle.get(child)
    merge = request("merge", moved, b, {moved: sha(root, moved), b: sha(root, b), a: sha(root, a)})
    z = tool(s, "okfy_reorganize", **merge)
    assert z["effect"] == "committed" and not bundle.get(moved)
    assert "/notes/b.md" in bundle.get(a).body and "Two [other]" in bundle.get(b).body
    assert git(root, "show", f"{z['commit']}^:{moved}.md")
    assert tool(s, "okfy_reorganize", **move) == {**m, "replayed": True}
    links = tool(s, "okfy_links", concept_id=a)
    assert b in links["out"]
    assert tool(s, "okfy_history", concept_id=moved)["events"][0]["action"] == "delete"


def test_dna_reachability(root):
    s = build_server(root)
    dna = sha(root, "dna")
    args = dict(concept_id="dna", expect_sha256=dna, actor=ACTOR,
                patch=[{"old": "Operator", "new": "Agent"}])
    proposal = tool(s, "okfy_write", **args)
    assert proposal["channel"] == "proposal" and proposal["persisted"]
    assert tool(s, "okfy_write", **{**args, "content": seed_text(), "patch": None,
                                     "expect_sha256": "absent"})["error"]
    for cid in ("notes/../dna", "./dna", "core/index", "notes/log", "meta/purpose",
                "proposals/dna", "/dna", "other/id"):
        assert "error" in tool(s, "okfy_write", **{**args, "concept_id": cid})
    assert tool(s, "okfy_propose", target_id="dna", action="delete", note="Fixture.",
                actor=ACTOR, supersedes="proposals/../dna")["error"]
    for action in ("split", "move", "merge"):
        for src, dst in (("dna", "notes/c"), ("notes/b", "dna")):
            assert "error" in tool(s, "okfy_reorganize", **request(
                action, src, dst, {src: dna, dst: "absent"},
                extract="Operator" if action == "split" else None))
    assert sha(root, "dna") == dna and git(root, "diff", "HEAD", "--", "dna.md") == b""
    # Pending DNA proposal need not be reviewed to write ordinary memory.
    assert tool(s, "okfy_write", concept_id="notes/new", expect_sha256="absent",
                actor=ACTOR, content="New.")["effect"] == "committed"


def seed_text():
    return "---\ntype: Memory\ntitle: Candidate\ndescription: Synthetic\n---\nCandidate."


def test_absent_dna_is_not_bootstrapped(root):
    git(root, "rm", "dna.md")
    git(root, "commit", "-qm", "no provisioned dna")
    s = build_server(root)
    out = tool(s, "okfy_write", concept_id="dna", expect_sha256="absent",
               actor=ACTOR, content=seed_text())
    assert out["channel"] == "proposal" and not (root / "dna.md").exists()


def test_protected_backlink_and_aliases(root):
    s = build_server(root)
    seed(root, "dna", "Read [A](/notes/a.md).")
    git(root, "commit", "-qam", "operator backlink")
    a, b, d = "notes/a", "notes/b", "notes/d"
    move = request("move", a, d, {a: sha(root, a), d: "absent", b: sha(root, b)})
    assert "dna" in tool(s, "okfy_reorganize", **move)["message"]
    (root / "notes/alias.md").symlink_to(root / "dna.md")
    os.link(root / "dna.md", root / "notes/hard.md")
    old = sha(root, "dna")
    for cid in ("notes/hard", "notes/alias"):
        assert tool(s, "okfy_write", concept_id=cid, actor=ACTOR, expect_sha256=old,
                    content="Replaced.")["error"]
    assert sha(root, "dna") == old


def test_rollback_and_uncertain_commit(root, monkeypatch):
    s = build_server(root)
    a, b = "notes/a", "core/new-dir/b"
    args = request("split", a, b, {a: sha(root, a), b: "absent"}, extract="One.")
    old = git(root, "rev-parse", "HEAD")
    real = proposals._commit

    def fail(*args, **kwargs):
        raise OSError("synthetic commit failure")

    monkeypatch.setattr(proposals, "_commit", fail)
    refused = tool(s, "okfy_reorganize", **args)
    assert refused["effect"] == "refused" and git(root, "rev-parse", "HEAD") == old
    assert git(root, "status", "--porcelain") == b"" and not (root / "core").exists()

    def lost(*args, **kwargs):
        real(*args, **kwargs)
        raise OSError("synthetic lost commit confirmation")

    monkeypatch.setattr(proposals, "_commit", lost)
    uncertain = tool(s, "okfy_reorganize", **args)
    assert uncertain["effect"] == "uncertain" and "persisted" not in uncertain
    done = tool(s, "okfy_reorganize", **args)
    assert done["effect"] == "committed" and done["replayed"]
    assert git(root, "status", "--porcelain") == b""


def test_dirty_and_uncommitted_state_is_not_a_receipt(root):
    a, dest = "notes/a", "core/b"
    s = build_server(root)
    seed(root, a, "Uncommitted.\n\nOther.")
    args = request("split", a, dest, {a: sha(root, a), dest: "absent"}, extract="Other.")
    assert "committed bytes" in tool(s, "okfy_reorganize", **args)["message"]
    git(root, "restore", "notes/a.md")
    args.update(extract="One.", bases={a: sha(root, a), dest: "absent"})
    memory.record(Bundle(root), "accept", actor=ACTOR, target=a, proposal=None,
                  action="update", content_sha256=sha(root, a), receipt={"id": args["operation_id"]})
    assert "unsettled ledger" in tool(s, "okfy_reorganize", **args)["message"]
    assert not (root / "core").exists()


def test_malformed_profile_does_not_enable_governing_types():
    purpose = {"direct": {"profile": MEMORY_PROFILE}, "write_policy": "proposals",
               "preedit": {"governing_types": ["Memory"]}}
    assert direct_policy(purpose)[1] and preedit_policy(purpose)[1]
    assert direct_policy({"write_policy": "proposals", "direct": {"profile": "typo"}})[1]


def test_ambiguous_partial_rollback_is_not_success(root, monkeypatch):
    s = build_server(root)
    a, dest = "notes/a", "core/child"
    start = sha(root, a)
    args = request("split", a, dest, {a: start, dest: "absent"}, extract="One.")
    atomic = proposals._atomic_write

    def fail_restore(path, data):
        if path == root / f"{a}.md" and evolution.digest(data) == start:
            raise OSError("synthetic failed restore")
        atomic(path, data)

    def fail_record(*args, **kwargs):
        raise OSError("synthetic ledger unavailable")

    monkeypatch.setattr(proposals, "_atomic_write", fail_restore)
    monkeypatch.setattr(memory, "record", fail_record)
    prior_head = git(root, "rev-parse", "HEAD")
    outcome = tool(s, "okfy_reorganize", **args)
    assert outcome["effect"] == "uncertain" and "persisted" not in outcome
    assert prior_head == git(root, "rev-parse", "HEAD") and sha(root, a) != start
    assert "error" in tool(s, "okfy_reorganize", **args)  # no invented receipt


def test_commit_without_complete_receipt_never_reconciles(root, monkeypatch):
    s = build_server(root)
    a, d = "notes/a", "notes/new"
    real_record = memory.record

    def missing_row(*args, **kwargs):
        if kwargs["target"] != d:
            return real_record(*args, **kwargs)

    monkeypatch.setattr(memory, "record", missing_row)
    args = request("split", a, d, {a: sha(root, a), d: "absent"}, extract="One.")
    for _ in range(2):
        out = tool(s, "okfy_reorganize", **args)
        assert out["effect"] == "uncertain" and "persisted" not in out
        assert (root / f"{d}.md").is_file()  # correct bytes, actor and commit marker are insufficient


def test_stale_merge_destination_staged_dna_and_unreadable_link_graph(root):
    s = build_server(root)
    a, b = "notes/a", "notes/b"
    args = request("merge", a, b, {a: sha(root, a), b: sha(root, b)})
    stale = tool(s, "okfy_reorganize", **{**args, "bases": {**args["bases"], b: "0"*64}})
    assert stale["error"] == "E_WRITE_BASE_MOVED" and stale["effect"] == "refused"
    alias = root / "link"
    alias.symlink_to(root / "notes", target_is_directory=True)
    assert "incomplete" in tool(s, "okfy_reorganize", **args)["message"]
    alias.unlink()
    old_head = git(root, "rev-parse", "HEAD")
    seed(root, "dna", "Operator change, staged but not committed.")
    git(root, "add", "dna.md")
    staged = git(root, "diff", "--cached")
    assert "index not clean" in tool(s, "okfy_reorganize", **args)["message"]
    assert git(root, "rev-parse", "HEAD") == old_head and git(root, "diff", "--cached") == staged


def test_proposal_partial_and_dangling_lock_cannot_provision_dna(root, monkeypatch):
    s = build_server(root)
    dna = sha(root, "dna")

    def fail(*args, **kwargs):
        raise OSError("synthetic proposal write failure")

    monkeypatch.setattr(memory, "record", fail)
    out = tool(s, "okfy_propose", target_id="dna", action="update", content=seed_text(),
               note="Candidate", actor=ACTOR)
    assert out["effect"] == "uncertain" and sha(root, "dna") == dna
    git(root, "rm", "dna.md")
    lock = root / ".okfy-cache/review.lock"
    lock.unlink()
    lock.symlink_to(root / "dna.md")
    out = tool(s, "okfy_write", concept_id="dna", expect_sha256="absent",
               actor=ACTOR, content=seed_text())
    assert out["effect"] == "refused" and not (root / "dna.md").exists()


def test_structural_uses_existing_write_scope_and_actor(root, monkeypatch):
    from types import SimpleNamespace

    from okfy_mcp import http
    s = build_server(root, shared=True)
    a, d = "notes/a", "notes/other"
    args = request("split", a, d, {a: sha(root, a), d: "absent"}, extract="One.")
    token = SimpleNamespace(claims={}, subject="human:test", scopes=[http.READ_SCOPE])
    monkeypatch.setattr(http, "get_access_token", lambda: token)
    assert tool(s, "okfy_reorganize", **args)["error"] == "E_MCP_FORBIDDEN"
    token.scopes.append(http.WRITE_SCOPE)
    assert tool(s, "okfy_reorganize", **args)["effect"] == "committed"
    assert tool(s, "okfy_history", concept_id=d)["events"][0]["actor"] == "human:test"


def test_retirement_refuses_open_target_anchor_or_unequal_provenance(root):
    s = build_server(root)
    a, b = "notes/a", "notes/b"

    def merge():
        return tool(s, "okfy_reorganize", **request("merge", a, b, {a: sha(root, a), b: sha(root, b)}))

    write = dict(concept_id=b, actor=ACTOR, evidence={"kind": "test-run", "ref": "synthetic:1"})
    assert tool(s, "okfy_write", **write, expect_sha256=sha(root, b),
                content="See [item](a.md#one).")["effect"] == "committed"
    assert "different evidence" in merge()["message"]
    assert tool(s, "okfy_write", **{**write, "concept_id": a}, expect_sha256=sha(root, a),
                content="One.\n\nTwo.")["effect"] == "committed"
    assert "anchored" in merge()["message"]
    assert tool(s, "okfy_propose", target_id=a, action="update", content=seed_text(),
                note="Pending", actor=ACTOR)["persisted"]
    assert "open proposal" in merge()["message"]


def test_same_base_concurrent_calls_cannot_both_land(root):
    from concurrent.futures import ThreadPoolExecutor

    s = build_server(root)
    a = "notes/a"
    base = sha(root, a)
    def submit(actor):
        return tool(s, "okfy_write", concept_id=a, actor=actor, operation_id=ident(),
                    expect_sha256=base, content="Written by " + actor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ("writer/1", "writer/2")))
    assert sorted(r["effect"] for r in results) == ["committed", "refused"]
    assert next(r for r in results if "error" in r)["error"] == "E_WRITE_BASE_MOVED"


def test_legacy_absent_lane_and_arbitrary_types(root):
    purpose = root / "meta/purpose.md"
    meta, body = frontmatter.parse(purpose.read_text())
    del meta["direct"]
    purpose.write_text(frontmatter.serialize(meta, body))
    s = build_server(root)
    assert "okfy_reorganize" not in [t.name for t in asyncio.run(s.list_tools())]
    args = dict(concept_id="dna", expect_sha256=sha(root, "dna"), actor=ACTOR, content=seed_text())
    assert tool(s, "okfy_write", **args)["channel"] == "proposal"
    meta["direct"] = {"types": ["Memory"]}
    purpose.write_text(frontmatter.serialize(meta, body))
    s = build_server(root)
    out = tool(s, "okfy_write", **args)
    assert out["channel"] == "direct" and "operation_id" not in out  # no universal DNA policy


def test_proposal_cannot_consume_dna_even_if_it_looks_like_its_lane(root):
    s = build_server(root)
    # Not a prefix-only negative: the normal supersedes actor/target checks
    # would both PASS. The effective path, not content, must deny the unlink.
    path = root / "dna.md"
    meta, body = frontmatter.parse(path.read_text())
    meta.update(generated={"by": ACTOR}, proposal={"target": "notes/a"})
    path.write_text(frontmatter.serialize(meta, body))
    args = dict(target_id="notes/a", action="update", content=seed_text(),
                note="Candidate", actor=ACTOR)
    genuine = tool(s, "okfy_propose", **args)["proposal"]
    before = path.read_bytes()
    for old in ("proposals/../dna", "proposals/dna-link"):
        (root / "proposals/dna-link.md").symlink_to(path)
        result = tool(s, "okfy_propose", **args, supersedes=old)
        assert result["effect"] == "refused" and path.read_bytes() == before
        (root / "proposals/dna-link.md").unlink()
    assert tool(s, "okfy_propose", **args, supersedes=genuine)["persisted"]
    assert path.read_bytes() == before


def test_proposal_operand_cannot_disguise_dna_even_on_later_review(root):
    alias = root / "notes/alias.md"
    s = build_server(root)
    args = dict(target_id="notes/alias", action="update", content=seed_text(),
                note="Candidate", actor=ACTOR)
    alias.symlink_to(root / "dna.md")
    assert tool(s, "okfy_propose", **args)["effect"] == "refused"
    alias.unlink()
    alias.write_bytes((root / "dna.md").read_bytes())
    prop = tool(s, "okfy_propose", **args)["proposal"]
    # Even with identical base bytes, an alias substituted after filing must
    # not turn an operator's review of notes/alias into a review of DNA.
    old = sha(root, "dna")
    alias.unlink()
    alias.symlink_to(root / "dna.md")
    with pytest.raises(ValueError, match="regular file"):
        proposals.accept(Bundle(root), prop)
    assert sha(root, "dna") == old


def test_stage_failure_rolls_back_index_and_all_paths(root):
    s = build_server(root)
    (root / ".gitignore").write_text(".okfy-cache/\ncore/ignored/\n")
    git(root, "commit", "-qam", "ignore one destination")
    a, d = "notes/a", "core/ignored/child"
    args = request("split", a, d, {a: sha(root, a), d: "absent"}, extract="One.")
    old_head = git(root, "rev-parse", "HEAD")
    assert tool(s, "okfy_reorganize", **args)["error"] == "E_MEMORY_ROLLBACK"
    assert git(root, "rev-parse", "HEAD") == old_head and not git(root, "status", "--porcelain")
    assert not (root / "core").exists()


def test_move_preserves_native_link_meaning_and_live_inventory(root):
    s = build_server(root)
    a, b, d = "notes/a", "notes/b", "core/depth/a"
    seed(root, a, "## One\nSee [neighbor](b.md) and [self](a.md#one) and [site](https://example.invalid).")
    seed(root, b, "Here is [one](a.md#one).")
    git(root, "commit", "-qam", "native links")
    args = request("move", a, d, {a: sha(root, a), b: sha(root, b), d: "absent"})
    assert tool(s, "okfy_reorganize", **args)["effect"] == "committed"
    bundle = Bundle(root)
    assert bundle.get(d).body == (
        "## One\nSee [neighbor](/notes/b.md) and [self](/core/depth/a.md#one) "
        "and [site](https://example.invalid).")
    assert bundle.get(b).body == "Here is [one](/core/depth/a.md#one)."
    listed = tool(s, "okfy_overview", listing=True)
    ids = {c["id"]: c["sha256"] for c in listed["concepts"]}
    assert ids[d] == sha(root, d) and a not in ids and not listed["unreadable"]
    assert not (root / "index.md").exists()


def test_packaged_memory_instructions_do_not_impose_an_archetype_layout(root):
    from okfy.commands.common import _archetype_for
    from okfy.package import _write_memory_protocol, render_agents_md
    b = Bundle(root)
    guidance = render_agents_md(b, _archetype_for(b))
    assert "dna is proposal-only" in guidance and "strategies/" not in guidance
    _write_memory_protocol(b, "proposals")
    assert "Choose the hierarchy" in (root / "protocols/memory.md").read_text()
