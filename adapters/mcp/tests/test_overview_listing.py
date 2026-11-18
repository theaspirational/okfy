"""okfy_overview(listing=true): the concepts as they are on disk now, with
the digest okfy_show returns — and what a reader that keeps a pack of
concepts can prove from the tools as they are, over the real stdio wire."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from okfy import frontmatter
from okfy.init import init_bundle
from okfy_mcp import handlers
from okfy_mcp.resolve import Target

ACTOR = "claude-code/1.0"
PLAN = """---
type: ExtractionPlan
title: Test plan
archetype: decision-support
archetype_version: 1
types:
  note: session notes
  area: standing areas
segments: []
---

Plan.
"""


def _concept(type_: str, title: str, body: str) -> str:
    return (f"---\ntype: {type_}\ntitle: {title}\n"
            f"description: About {title}.\n---\n\n{body}\n")


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
    (root / "areas").mkdir()
    (root / "notes/a.md").write_text(_concept("note", "A", "First line."), encoding="utf-8")
    (root / "notes/b.md").write_text(_concept("note", "B", "Bee."), encoding="utf-8")
    (root / "areas/team.md").write_text(_concept("area", "Team", "Nobody."), encoding="utf-8")
    big = "\n".join(f"Line {i:05d} of a long synthetic body." for i in range(3000))
    (root / "notes/big.md").write_text(_concept("note", "Big", big), encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-q", "--no-verify", "-m", "fixture")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _listing(t, **kw):
    return handlers.safe_call(handlers.h_overview, t, listing=True, **kw)


def _file_sha(root: Path, cid: str) -> str:
    return hashlib.sha256((root / f"{cid}.md").read_bytes()).hexdigest()


def test_listing_is_every_concept_with_the_show_digest(root):
    t = Target(root)
    out = _listing(t, max_items=1000)
    ids = [c["id"] for c in out["concepts"]]
    assert ids == sorted(ids) and out["total"] == len(ids)
    assert {"areas/team", "notes/a", "notes/b", "notes/big"} <= set(ids)
    assert out["unreadable"] == [] and "next_offset" not in out
    for c in out["concepts"]:
        assert set(c) == {"id", "type", "title", "description", "sha256"}
        assert c["sha256"] == _file_sha(root, c["id"])
        assert c["sha256"] == handlers.h_show(t, concept_id=c["id"])["sha256"]
    a = next(c for c in out["concepts"] if c["id"] == "notes/a")
    assert (a["type"], a["title"], a["description"]) == ("note", "A", "About A.")
    # same bundle, same answer: the listing is a function of the files
    assert _listing(t, max_items=1000) == out


def test_listing_sees_a_direct_create_the_index_text_does_not(root):
    t = Target(root)
    before = _listing(t, max_items=1000)
    made = handlers.h_write(t, "notes/new", actor=ACTOR, expect_sha256="absent",
                            content=_concept("note", "New", "Fresh."))
    assert made["channel"] == "direct"
    after = _listing(t, max_items=1000)
    new = next(c for c in after["concepts"] if c["id"] == "notes/new")
    assert new["sha256"] == made["sha256"]
    assert after["total"] == before["total"] + 1
    assert after["listing_sha256"] != before["listing_sha256"]
    # the negative control this listing exists for: index.md is not rewritten
    assert "notes/new" not in handlers.h_overview(t)["index"]


def test_listing_sha256_moves_with_any_listed_change_and_only_then(root):
    t = Target(root)
    one = _listing(t, max_items=1)
    assert _listing(t, max_items=1000)["listing_sha256"] == one["listing_sha256"]
    assert _listing(t, type_="note")["listing_sha256"] != one["listing_sha256"]
    sha = handlers.h_show(t, concept_id="notes/a")["sha256"]
    handlers.h_write(t, "notes/a", actor=ACTOR, expect_sha256=sha,
                     patch=[{"old": "First line.", "new": "Changed."}])
    assert _listing(t, max_items=1)["listing_sha256"] != one["listing_sha256"]


def test_a_proposal_is_not_listed(root):
    t = Target(root)
    before = _listing(t, max_items=1000)
    sha = handlers.h_show(t, concept_id="areas/team")["sha256"]
    filed = handlers.h_write(t, "areas/team", actor=ACTOR, expect_sha256=sha,
                             patch=[{"old": "Nobody.", "new": "Somebody."}])
    assert filed["channel"] == "proposal" and filed["persisted"] is True
    after = _listing(t, max_items=1000)
    assert after == before        # the live text did not move
    assert not any(c["id"].startswith("proposals/") for c in after["concepts"])


def test_listing_pages_and_filters(root):
    t = Target(root)
    full = _listing(t, max_items=1000)
    seen, offset = [], 0
    while True:
        page = _listing(t, max_items=2, offset=offset)
        assert page["total"] == full["total"]
        assert page["listing_sha256"] == full["listing_sha256"]
        seen += page["concepts"]
        if "next_offset" not in page:
            break
        offset = page["next_offset"]
    assert seen == full["concepts"]
    assert _listing(t, offset=10_000)["concepts"] == []
    notes = _listing(t, type_="note")
    assert [c["id"] for c in notes["concepts"]] == ["notes/a", "notes/b", "notes/big"]
    assert notes["total"] == 3
    assert _listing(t, type_="no-such-type")["total"] == 0


def test_an_unreadable_concept_is_named_not_dropped(root):
    (root / "notes/broken.md").write_text("---\ntype: note\nno closing fence\n",
                                          encoding="utf-8")
    (root / "notes/latin.md").write_bytes(b"---\ntype: note\n---\n\n\xff\xfe\n")
    out = _listing(Target(root), max_items=1000)
    assert out["unreadable"] == ["notes/broken", "notes/latin"]
    assert not {"notes/broken", "notes/latin"} & {c["id"] for c in out["concepts"]}
    assert "notes/a" in {c["id"] for c in out["concepts"]}


def test_listing_refusals(root):
    t = Target(root)
    for kw in ({"offset": -1}, {"max_items": 0}, {"max_items": True},
               {"offset": "3"}):
        bad = _listing(t, **kw)
        assert bad["error"] == "E_OVERVIEW_LISTING_PAGE" and bad["way_out"]
    both = _listing(t, shard="notes")
    assert both["error"] == "E_OVERVIEW_LISTING_SHARD" and both["way_out"]


def test_existing_overview_shapes_are_untouched(root):
    """listing defaults to false: the type listing keeps its four keys and no
    digest, the index text its page shape."""
    t = Target(root)
    typed = handlers.h_overview(t, type_="note")
    assert set(typed) == {"concepts", "total"}
    assert all(set(c) == {"id", "type", "title", "description"}
               for c in typed["concepts"])
    text = handlers.h_overview(t)
    assert "index" in text and "total_chars" in text and "concepts" not in text


# ---- the same contract over the wire: a real stdio server, the SDK client ----

def _call_all(root: Path, calls: list[tuple[str, dict]]) -> tuple[list, list]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "okfy_mcp", "serve", str(root)],
            env=dict(os.environ))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = (await s.list_tools()).tools
                results = [await s.call_tool(name, args) for name, args in calls]
                return tools, results
    return asyncio.run(go())


def _data(result) -> dict:
    """What a host has to do with a result: one text block, JSON inside."""
    assert result.isError is False
    assert [b.type for b in result.content] == ["text"]
    return json.loads(result.content[0].text)


def test_wire_contract_for_a_pack_reader(root, monkeypatch):
    src = Path(handlers.__file__).resolve().parents[1]
    core = Path(frontmatter.__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [str(src), str(core), os.environ.get("PYTHONPATH", "")]))
    sha_a = _file_sha(root, "notes/a")
    tools, res = _call_all(root, [
        ("okfy_overview", {"listing": True, "max_items": 1000}),            # 0
        ("okfy_show", {"concept_id": "notes/big"}),                         # 1
        ("okfy_show", {"concept_id": "notes/big", "max_chars": 10_000_000}),  # 2
        ("okfy_show", {"concept_ids": ["notes/a", "notes/big", "notes/nope"]}),  # 3
        ("okfy_show", {"concept_ids": ["notes/a", "notes/big", "notes/nope"],
                       "max_tokens": 10_000_000}),                          # 4
        ("okfy_write", {"concept_id": "notes/a", "actor": ACTOR,
                        "expect_sha256": sha_a,
                        "patch": [{"old": "First line.", "new": "Second."}]}),  # 5
        ("okfy_write", {"concept_id": "notes/a", "actor": "codex/1.0",
                        "expect_sha256": sha_a,
                        "patch": [{"old": "First line.", "new": "Lost."}]}),  # 6
        ("okfy_fresh", {"ids": {"notes/a": sha_a, "notes/b": _file_sha(root, "notes/b")}}),  # 7
        ("okfy_overview", {"listing": True, "max_items": 0}),               # 8
        ("okfy_overview", {"type": "note"}),                                # 9
    ])
    names = [t.name for t in tools]
    assert names == ["okfy_query", "okfy_show", "okfy_links", "okfy_preedit",
                     "okfy_overview", "okfy_fresh", "okfy_diff", "okfy_history",
                     "okfy_changes", "okfy_propose", "okfy_write"]
    overview = next(t for t in tools if t.name == "okfy_overview")
    assert overview.inputSchema["properties"]["listing"] == {
        "default": False, "title": "Listing", "type": "boolean"}
    assert "listing" not in overview.inputSchema.get("required", [])

    listing = _data(res[0])
    by_id = {c["id"]: c for c in listing["concepts"]}
    assert by_id["notes/a"]["sha256"] == sha_a and listing["unreadable"] == []

    # a whole body, and the proof that it is whole
    cut, whole = _data(res[1]), _data(res[2])
    assert cut["truncated"] is True and "truncated" not in whole
    assert whole["sha256"] == by_id["notes/big"]["sha256"]
    assert hashlib.sha256(whole["content"].encode("utf-8")).hexdigest() == whole["sha256"]
    assert hashlib.sha256(cut["content"].encode("utf-8")).hexdigest() != cut["sha256"]
    many_cut, many = _data(res[3]), _data(res[4])
    assert {c["id"]: c["view"] for c in many_cut["concepts"]} == {
        "notes/a": "full", "notes/big": "truncated"}
    assert {c["id"]: c["view"] for c in many["concepts"]} == {
        "notes/a": "full", "notes/big": "full"}
    assert many["missing"] == ["notes/nope"] and many["omitted"] == []

    # compare-and-swap: one writer lands, the other is refused as data
    won, lost = _data(res[5]), _data(res[6])
    assert won["persisted"] is True and won["channel"] == "direct"
    assert won["sha256"] != sha_a
    assert lost["error"] == "E_WRITE_BASE_MOVED" and "persisted" not in lost
    assert lost["current_sha256"] == won["sha256"] and lost["changed_by"] == ACTOR
    assert (root / "notes/a.md").read_text(encoding="utf-8").count("Lost.") == 0

    # okfy_fresh stays a flat {id: state} map
    assert _data(res[7]) == {"notes/a": "changed", "notes/b": "unchanged"}
    assert _data(res[8])["error"] == "E_OVERVIEW_LISTING_PAGE"
    assert set(_data(res[9])) == {"concepts", "total"}


# ---- review regressions: real filesystem failures and the real serializer ----

def _review_wire(root, label, calls):
    """Retain raw serializer results when dispatched with an evidence directory."""
    _, results = _call_all(root, calls)
    evidence = os.environ.get("ORGASMIC_EVIDENCE_DIR")
    if evidence:
        (Path(evidence) / f"listing-{label}.json").write_text(json.dumps(
            [r.model_dump(mode="json") for r in results], indent=2), encoding="utf-8")
    return [_data(r) for r in results]


def test_wire_denied_directory_is_incomplete_and_recovers(root):
    # This must be a real unprivileged negative control, not a mock or a skip.
    assert os.geteuid() != 0, "run the permission regression as an ordinary user"
    sealed = root / "sealed"
    sealed.mkdir()
    (sealed / "hidden.md").write_text(_concept("note", "Hidden", "Inside."))
    call = [("okfy_overview", {"listing": True, "max_items": 1000})]
    before, = _review_wire(root, "permission-before", call)
    assert before["unreadable"] == []
    assert "sealed/hidden" in {c["id"] for c in before["concepts"]}
    try:
        sealed.chmod(0)
        with pytest.raises(PermissionError):
            list(sealed.iterdir())
        after, filtered = _review_wire(root, "permission-denied", call + [
            ("okfy_overview", {"listing": True, "type": "no-such-type"})])
        assert after["unreadable"] == filtered["unreadable"] == ["sealed/"]
        assert before["total"] == after["total"] + 1
        assert before["listing_sha256"] != after["listing_sha256"]
        assert "notes/a" in {c["id"] for c in after["concepts"]}
    finally:
        sealed.chmod(0o755)
    recovered, = _review_wire(root, "permission-recovered", call)
    assert recovered == before


def test_wire_symlinks_refused_before_read_and_ordinary_files_survive(root, monkeypatch):
    outside = root.parent / "synthetic-outside.md"
    outside.write_text(_concept("note", "OUTSIDE SENTINEL", "Synthetic only."))
    outside_dir = root.parent / "synthetic-directory"
    outside_dir.mkdir()
    (outside_dir / "hidden.md").write_text(_concept("note", "Outside dir", "Body."))
    (root / "notes/escape.md").symlink_to(outside)
    (root / "notes/alias.md").symlink_to(root / "notes/a.md")
    (root / "external").symlink_to(outside_dir, target_is_directory=True)
    (root / "internal").symlink_to(root / "notes", target_is_directory=True)
    (root / "notes/dangling.md").symlink_to(root / "missing.md")
    (root / "loop").symlink_to(root / "loop")
    original = handlers._read_concept

    def guarded(path):
        assert not path.is_symlink()
        assert path.resolve().is_relative_to(root)
        return original(path)

    monkeypatch.setattr(handlers, "_read_concept", guarded)
    direct = _listing(Target(root), max_items=1000)
    listing, show = _review_wire(root, "symlinks", [
        ("okfy_overview", {"listing": True, "max_items": 1000}),
        ("okfy_show", {"concept_id": "notes/escape"})])
    assert listing == direct
    assert listing["unreadable"] == [
        "external/", "internal/", "loop/", "notes/alias", "notes/dangling", "notes/escape"]
    assert show["error"] == "E_MCP_REFUSAL"
    assert "OUTSIDE SENTINEL" not in json.dumps(listing)
    assert next(c for c in listing["concepts"] if c["id"] == "notes/a")["sha256"] == _file_sha(root, "notes/a")


@pytest.mark.parametrize("label,metadata", [
    ("binary", "title: !!binary /w=="),
    ("complex-key", "? [a, b]\n: value"),
    ("date", "title: 2026-11-18"),
    ("list", "description: [a, b]"),
    ("mapping", "title: {a: b}"),
    ("null-title", "title: null"),
    ("number-type", "type: 42"),
    ("surrogate", 'title: "\\uD800"'),
])
def test_wire_unsupported_listing_metadata_is_per_file(root, label, metadata):
    # Dates/list values are intentionally unsupported by the NEW listing's
    # string policy; this does not claim the SDK cannot serialize them.
    (root / "notes/bad.md").write_text(f"---\n{metadata}\n---\nBody.\n")
    full, filtered = _review_wire(root, f"metadata-{label}", [
        ("okfy_overview", {"listing": True, "max_items": 1000}),
        ("okfy_overview", {"listing": True, "type": "no-such-type"})])
    assert full["unreadable"] == filtered["unreadable"] == ["notes/bad"]
    assert "notes/a" in {c["id"] for c in full["concepts"]}
    assert "notes/bad" not in {c["id"] for c in full["concepts"]}
    assert filtered["concepts"] == []


def test_wire_listing_defaults_and_excluded_directories(root):
    (root / "notes/defaults.md").write_text("---\nother: [1, 2]\n---\nBody.\n")
    # Root-only concept exclusions must be pruned BEFORE enumeration. Denied
    # excluded subtrees should not make an otherwise healthy scan incomplete.
    excluded = [root / name for name in
                (".git", "drafts", "proposals", ".okfy-cache", "index", "protocols")]
    for directory in excluded:
        directory.mkdir(exist_ok=True)
    try:
        for directory in excluded:
            directory.chmod(0)
        listing, = _review_wire(root, "defaults-exclusions", [
            ("okfy_overview", {"listing": True, "max_items": 1000})])
        assert listing["unreadable"] == []
        row = next(c for c in listing["concepts"] if c["id"] == "notes/defaults")
        assert row == {"id": "notes/defaults", "type": None, "title": "",
                       "description": "", "sha256": _file_sha(root, "notes/defaults")}
    finally:
        for directory in excluded:
            directory.chmod(0o755)


def test_listing_aba_is_not_a_history_fence(root):
    t = Target(root)
    before = _listing(t)
    p = root / "notes/a.md"
    original = p.read_bytes()
    p.write_text(_concept("note", "Temporary", "Intervening edit."))
    during = _listing(t)
    p.write_bytes(original)
    after = _listing(t)
    assert before["listing_sha256"] != during["listing_sha256"]
    assert before == after  # Equal observations do not prove no edit BETWEEN.


def test_listing_discovery_keeps_legacy_concept_exclusions(root):
    # Exclusions are root-only except reserved filenames. Do not accidentally
    # prune nested .git/drafts or directories merely named like reserved files.
    for rel in ("notes/.git", "notes/drafts", "log.md", "README.md"):
        directory = root / rel
        if directory.is_file():
            directory.unlink()  # synthetic generated root doc, replaced by a directory
        directory.mkdir()
        (directory / "nested.md").write_text(_concept("note", "Nested", "Body."))
    t = Target(root)
    expected = sorted(t.bundle.concept_id(p) for p in t.bundle.iter_md_files())
    listing = _listing(t, max_items=1000)
    assert listing["unreadable"] == []
    assert [c["id"] for c in listing["concepts"]] == expected


def test_mapping_key_errors_are_frontmatter_errors_not_arbitrary_type_errors():
    for yaml in ("? [a, b]\n: value", "? {a: b}\n: value", "title: A\ntitle: B"):
        with pytest.raises(frontmatter.FrontmatterError):
            frontmatter.parse(f"---\n{yaml}\n---\nBody.\n")
    assert frontmatter.parse("---\ntitle: Good\nextra: {a: b}\n---\nBody.\n")[0] == {
        "title": "Good", "extra": {"a": "b"}}
