"""Own Okapi BM25 — no embeddings, no deps (ADR-0001, ADR-0004)."""
import math
import re

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


# Dropped from the QUERY only (query.search_pool): the index keeps every
# token, so no bundle's index fingerprint moves. In a small corpus a word
# like "who" carries real idf and dragged an unrelated concept to rank 2.
# ponytail: English only; add a language's words when an eval run shows the need
STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how in is it of on or "
    "that the this to was what which who with".split())


def query_terms(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in STOPWORDS]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.N = len(docs)
        self.doc_len = [len(d) for d in docs]
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0
        self.tf: list[dict[str, int]] = []
        self.df: dict[str, int] = {}
        for d in docs:
            counts: dict[str, int] = {}
            for t in d:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            for t in counts:
                self.df[t] = self.df.get(t, 0) + 1

    def idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    def score(self, query_tokens: list[str], i: int) -> float:
        s = 0.0
        for t in query_tokens:
            f = self.tf[i].get(t, 0)
            if not f:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / (self.avgdl or 1.0))
            s += self.idf(t) * f * (self.k1 + 1) / denom
        return s

    def search(self, query_tokens: list[str], n: int = 10) -> list[tuple[int, float]]:
        scored = [(i, self.score(query_tokens, i)) for i in range(self.N)]
        scored = [x for x in scored if x[1] > 0.0]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:n]
