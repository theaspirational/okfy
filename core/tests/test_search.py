"""Retrieval: stopwords leave the query, and every hit names the query terms
it holds — anywhere in it, body included."""
import pytest

from okfy.bundle import Bundle
from okfy.query import query


def _concept(root, rel, title, description, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: Note\ntitle: {title}\ndescription: {description}\n"
                 f"---\n\n{body}\n", encoding="utf-8")


@pytest.fixture
def bundle(tmp_path) -> Bundle:
    root = tmp_path / "b"
    _concept(root, "notes/recipe.md", "Support desk recipe",
             "The setup Rook runs on.", "The agent holds its own login for the FTP.")
    _concept(root, "notes/owner.md", "Vasyl (owner)",
             "Who Vasyl is and how agents work with him.", "The profile.")
    return Bundle(root)


def test_stopwords_never_match(bundle):
    assert query(bundle, "who is the")["results"] == []
    ids = [h["id"] for h in query(bundle, "who issued the FTP login")["results"]]
    assert ids == ["notes/recipe"]          # `who` no longer pulls the owner in


def test_matched_terms_cover_the_body(bundle):
    (hit,) = query(bundle, "who issued the FTP login")["results"]
    assert hit["matched_terms"] == ["ftp", "login"]   # both live in the body only
