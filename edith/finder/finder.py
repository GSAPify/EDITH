"""NL repo finder (spec 09 §NL finder).

``find_repos(query, store, k)`` finds the owner's repos most relevant to a
natural-language query by fusing TWO signals over the ingested Memory graph:

- **semantic** — ``store.semantic_recall(query)`` (sqlite-vec KNN over Fact
  embeddings) when the store carries vectors. Ingest now writes through
  ``VectorMemoryStore`` so ingested Facts ARE embedded; older graph-only Facts
  are backfilled via ``VectorMemoryStore.backfill_embeddings`` (``--reembed``).
  Still degrades to empty on a truly vector-less store, so the graph signal
  below always carries the result.
- **graph** — ``store.recall(query)`` (case-insensitive substring scan + 1-hop
  traversal), which matches Repo names/summaries and related Facts directly.
  When BOTH signals are empty on a populated graph (e.g. a multi-word query with
  no verbatim substring), a per-token text fallback keeps a result surfacing.

Candidate Facts (and directly-matched Repos) are walked along ``relates_to``
edges to their Repo nodes using the store's ``graph_snapshot`` (which already
carries the edges + a per-node ``degree``). Repos are ranked by a blend of
match strength and graph degree. The ranking is DETERMINISTIC and MODEL-FREE —
no Router is needed. A separate ``summarize_hits`` optionally phrases a natural
answer over the top hits with an injected Router (Sonnet, latency-first).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from edith.memory.secrets import sanitize_text
from edith.router import ModelResponse, Tier

# Blend weights: semantic/graph match strength dominates; degree is a tie-break
# nudge so a well-connected repo edges out an equally-matched isolated one.
_DEGREE_WEIGHT = 0.1
_SUMMARY_MAX_TOKENS = 256

_SUMMARY_SYSTEM = (
    "You are EDITH. In one or two sentences, tell the owner which of their repos "
    "match their question, using only the ranked candidates below. Be concise."
)


@runtime_checkable
class StoreLike(Protocol):
    """The slice of the Memory contract the finder reads."""

    def recall(self, query: str) -> list[dict[str, object]]: ...

    def graph_snapshot(self) -> dict[str, list[dict[str, Any]]]: ...


@runtime_checkable
class VectorStoreLike(StoreLike, Protocol):
    """A store that also carries a semantic (vector) index."""

    def semantic_recall(self, query: str, k: int = ...) -> list[dict[str, object]]: ...


class RouterLike(Protocol):
    """The slice of the Router contract ``summarize_hits`` uses (spec 05 §4.3)."""

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = ...,
    ) -> ModelResponse: ...


@dataclass(frozen=True)
class RepoHit:
    """One ranked repo result."""

    name: str
    repo_id: str
    summary: str
    score: float
    degree: int


def find_repos(query: str, store: StoreLike, k: int = 5) -> list[RepoHit]:
    """Return up to ``k`` repos most relevant to ``query``, ranked, model-free."""
    snapshot = store.graph_snapshot()
    nodes = snapshot.get("nodes", [])
    links = snapshot.get("links", [])

    repo_by_id = {n["id"]: n for n in nodes if n.get("type") == "Repo"}
    # Fact -> Repo via relates_to (relates_to also fans to Person/Project/PR;
    # keep only the Repo targets so the walk always lands on a repo).
    fact_to_repos: dict[str, list[str]] = {}
    for link in links:
        if link.get("type") != "relates_to":
            continue
        target = str(link.get("target", ""))
        if target in repo_by_id:
            fact_to_repos.setdefault(str(link.get("source", "")), []).append(target)

    # Accumulate a match score per repo id from both signals.
    scores: dict[str, float] = {}

    # Semantic signal (only if the store has a vector index).
    if isinstance(store, VectorStoreLike):
        for rank, hit in enumerate(store.semantic_recall(query, k=max(k * 4, 10))):
            fact_id = str(hit.get("id", ""))
            # sqlite-vec returns L2 distance (smaller = closer); convert to a
            # descending strength, and fall back to rank if distance is absent.
            distance = hit.get("distance")
            if isinstance(distance, (int, float)):
                strength = 1.0 / (1.0 + float(distance))
            else:
                strength = 1.0 / (rank + 1)
            for repo_id in fact_to_repos.get(fact_id, []):
                scores[repo_id] = scores.get(repo_id, 0.0) + strength

    # Graph signal: substring recall over Repo names/summaries AND related Facts.
    for hit in store.recall(query):
        label = str(hit.get("label", ""))
        node_id = str(hit.get("id", ""))
        if label == "Repo" and node_id in repo_by_id:
            scores[node_id] = scores.get(node_id, 0.0) + 1.0
        elif label == "Fact":
            for repo_id in fact_to_repos.get(node_id, []):
                scores[repo_id] = scores.get(repo_id, 0.0) + 0.5

    # Graceful degradation: ``store.recall`` scans the WHOLE query as one
    # substring, so a multi-word NL query ("seo tools") silently misses a
    # populated graph where the tokens appear separately. When nothing matched
    # (no vectors AND no verbatim substring), fall back to a per-token text
    # match so a populated graph never returns nothing.
    if not scores:
        _token_fallback(query, nodes, repo_by_id, fact_to_repos, scores)

    ranked: list[RepoHit] = []
    for repo_id, score in scores.items():
        node = repo_by_id[repo_id]
        degree = int(node.get("degree", 0) or 0)
        ranked.append(
            RepoHit(
                name=str(node.get("name") or node.get("label") or repo_id),
                repo_id=repo_id,
                summary=str(node.get("summary") or ""),
                score=score + _DEGREE_WEIGHT * degree,
                degree=degree,
            )
        )

    ranked.sort(key=lambda h: (h.score, h.name), reverse=True)
    return ranked[:k]


def _token_fallback(
    query: str,
    nodes: list[dict[str, Any]],
    repo_by_id: dict[str, dict[str, Any]],
    fact_to_repos: dict[str, list[str]],
    scores: dict[str, float],
) -> None:
    """Per-token text match over Repo name/summary + Fact.text into ``scores``.

    The design-around for ``store.recall``'s whole-query substring scan: match
    each query token independently, so a repo whose name/summary/related-Fact
    shares any token still surfaces. Score is proportional to token overlap.
    """
    tokens = {t for t in query.lower().split() if len(t) > 2}
    if not tokens:
        return

    def overlap(text: str) -> int:
        blob = text.lower()
        return sum(1 for t in tokens if t in blob)

    for node in nodes:
        node_id = str(node.get("id", ""))
        node_type = str(node.get("type", ""))
        if node_type == "Repo" and node_id in repo_by_id:
            text = f"{node.get('name', '')} {node.get('summary', '')}"
            hits = overlap(text)
            if hits:
                scores[node_id] = scores.get(node_id, 0.0) + hits
        elif node_type == "Fact":
            hits = overlap(str(node.get("text", "")))
            if hits:
                for repo_id in fact_to_repos.get(node_id, []):
                    scores[repo_id] = scores.get(repo_id, 0.0) + 0.5 * hits


async def summarize_hits(query: str, hits: list[RepoHit], router: RouterLike) -> str:
    """Phrase a natural one-liner over the top hits (Sonnet, latency-first).

    No hits -> a plain message, no model call (cost discipline). The candidate
    blob is redacted before the call as defence-in-depth, even though repo
    summaries should already be clean.
    """
    if not hits:
        return "No matching repos found in memory."

    lines = [f"- {h.name}: {h.summary}".rstrip(": ") for h in hits]
    content = sanitize_text(
        f"{_SUMMARY_SYSTEM}\n\nQuestion: {query}\n\nRanked candidates:\n" + "\n".join(lines)
    )
    messages: list[dict[str, object]] = [{"role": "user", "content": content}]
    response = await router.model_call(messages, Tier.SONNET, max_tokens=_SUMMARY_MAX_TOKENS)
    return response.text
