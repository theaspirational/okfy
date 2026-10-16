"""Retrieval: lexicon-expanded BM25 over the index (ADR-0013). Accepted lexicon
rows pin their maps_to concepts first (via: lexicon); BM25 over the expanded
query fills the rest. Stale concepts stay visible but marked — no demotion."""
from okfy import lexicon
from okfy.bm25 import BM25, query_terms, tokenize
from okfy.bundle import Bundle, Concept
from okfy.index import load_index
from okfy.validate import external_links


def _hit(c: dict, score: float | None = None) -> dict:
    h = {"id": c["id"], "type": c["type"], "title": c["title"],
         "description": c["description"], "score": score}
    if c.get("stale"):
        h["stale"] = True
        h["stale_reason"] = c.get("stale_reason", "")
    return h


def filter_pool(idx: dict, type_: str | None = None, tag: str | None = None,
                include_meta: bool = False, include_stale: bool = True) -> list[dict]:
    return [c for c in idx["concepts"]
            if (include_meta or not c["id"].startswith("meta/"))
            and (type_ is None or c["type"] == type_)
            and (tag is None or tag in c["tags"])
            and (include_stale or not c.get("stale"))]


def domain_tokens(pool: list[dict], text: str) -> frozenset[str]:
    """Stopwords the query uses as a NAME: when the whole query, token for
    token, is some concept's title or alias in the pool, its tokens are never
    dropped as stopwords (bm25.query_terms), so a concept whose alias is
    exactly "and" or "as of" stays reachable. Anything less keeps them noise:
    a name merely occurring in the query ("who is the" beside a title "The",
    "The Rules of the Road") would turn `the` into a scoring term for every
    body. Identifiers are not indexed (index.concept_tokens), so protecting
    an id slug would only add noise."""
    q = tokenize(text)
    for c in pool:
        for name in [c.get("title") or "", *(c.get("aliases") or [])]:
            if q and tokenize(str(name)) == q:
                return frozenset(q)
    return frozenset()


def search_pool(pool: list[dict], pins: list[str], text: str, n: int,
                raw: str | None = None) -> list[dict]:
    """Pinned concepts first (via: lexicon, no score); BM25 over the rest of
    the pool fills the remainder of n. Pins outside the pool are dropped.
    Every hit carries `matched_terms`: the query's terms (stopwords dropped
    unless the whole query is a title or alias in the pool; lexicon
    expansion included) found anywhere in the concept — title,
    aliases, tags, description or body. `raw` is the user's own query when
    `text` is its expansion: the whole-query name check compares the words
    the user typed, not what a lexicon or crosswalk appended."""
    by_id = {c["id"]: c for c in pool}
    qt = query_terms(text, domain_tokens(pool, text if raw is None else raw))
    results = [dict(_hit(by_id[p]), via="lexicon") for p in pins if p in by_id][:n]
    rest = [c for c in pool if c["id"] not in set(pins)]
    bm = BM25([c["tokens"] for c in rest])
    results += [_hit(rest[i], round(s, 3))
                for i, s in bm.search(qt, max(0, n - len(results)))]
    for h in results:
        h["matched_terms"] = sorted(set(qt) & set(by_id[h["id"]]["tokens"]))
    return results


def query(bundle: Bundle, text: str, type_: str | None = None, tag: str | None = None,
          n: int = 10, include_meta: bool = False, expand: bool = True,
          include_stale: bool = True) -> dict:
    pool = filter_pool(load_index(bundle), type_, tag, include_meta, include_stale)
    eff = lexicon.expand(lexicon.load_rows(bundle) if expand else [], text)
    return {"results": search_pool(pool, eff["pins"], eff["expanded_query"], n, raw=text),
            "expanded_query": eff["expanded_query"], "notes": eff["notes"]}


def show(bundle: Bundle, concept_id: str) -> Concept:
    c = bundle.get(concept_id)
    if c is None:
        raise KeyError(f"concept not found: {concept_id}")
    return c


def links(bundle: Bundle, concept_id: str) -> dict:
    idx = load_index(bundle)
    by_id = {c["id"]: c for c in idx["concepts"]}
    if concept_id not in by_id:
        raise KeyError(f"concept not found in index: {concept_id}")
    out = by_id[concept_id]["links"]
    backlinks = sorted(c["id"] for c in idx["concepts"] if concept_id in c["links"])
    # Read from the page, not the index: adding it there would move the
    # retrieval fingerprint every recorded eval is pinned to.
    external = external_links(show(bundle, concept_id).body)
    return {"id": concept_id, "out": out, "backlinks": backlinks, "external": external}
