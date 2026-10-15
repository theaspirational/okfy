"""`okfy_query`'s `matched` reads the core's matched_terms (body included,
lexicon expansion marked); `okfy_show(outline=true)` is headings + sha only."""
from pathlib import Path

import pytest

from okfy.init import init_bundle
from okfy_mcp import handlers
from okfy_mcp.resolve import Target

RECIPE = """---
type: Note
title: Support desk recipe
description: The setup Rook runs on.
---

Intro.

## Logins

The agent holds its own login for the FTP.

## Where to look

The mirror first.
"""
LEXICON = """---
type: Lexicon
title: Lexicon
rows:
- term: rook
  status: accepted
  maps_to: [notes/recipe]
  canonical_terms: [mirror]
---

| term | maps_to |
| --- | --- |
| rook | notes/recipe |
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
    (root / "notes").mkdir()
    (root / "notes/recipe.md").write_text(RECIPE, encoding="utf-8")
    (root / "meta/lexicon.md").write_text(LEXICON, encoding="utf-8")
    return root


def test_matched_comes_from_the_whole_concept(root):
    out = handlers.h_query(Target(root), "who issued the FTP login for rook")
    (hit,) = out["results"]
    assert hit["id"] == "notes/recipe" and hit["via"] == "lexicon"
    assert hit["matched"] == {"terms": ["ftp", "login", "mirror", "rook"],
                              "via_lexicon": ["mirror"]}
    assert "matched_terms" not in hit


def test_outline_is_headings_and_sha_only(root):
    t = Target(root)
    full = handlers.h_show(t, concept_id="notes/recipe")
    outline = handlers.h_show(t, concept_id="notes/recipe", outline=True)
    assert set(outline) == {"id", "sha256", "sections", "notice"}
    assert outline["sections"] == ["Logins", "Where to look"]
    assert outline["sha256"] == full["sha256"]


def test_server_instructions_name_the_gap_door(root):
    from okfy_mcp.server import build_server
    text = build_server(root).instructions
    assert "okfy_propose" in text and "gap" in text


def test_empty_search_says_what_to_do(root):
    out = handlers.h_query(Target(root), "zzqqxx")
    assert out["results"] == [] and out["empty_reason"] == "no-term-matched"
    assert "okfy_propose" in out["way_out"] and "gap" in out["way_out"]
