"""Seed a temp Kuzu DB with a realistic sample graph for ``--demo``.

Generic, plausible content only — no real secrets, no real tokens. Produces
~120-160 nodes (Projects -> Repos -> PRs -> People -> Facts) with authored_by,
reviewed_by, owns, and relates_to edges so the viewer shows a dense cloud.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from edith.memory.store import MemoryStore

from edith.memory.store import Edge, Node

_PROJECTS = [
    ("onboarding-portal", "active"),
    ("catalog-sync", "active"),
    ("pricing-engine", "paused"),
    ("review-bot", "active"),
    ("ingest-pipeline", "active"),
    ("dashboard-ui", "planning"),
]

_PEOPLE = [
    "Nate", "Priya", "Marcus", "Sofia", "Deepak", "Lena",
    "Omar", "Grace", "Ravi", "Elena", "Tomás", "Aisha",
]

_PR_TITLES = [
    "Fix flaky retry in sync job",
    "Add pagination to catalog endpoint",
    "Redact tokens before persist",
    "Wire up webhook handler",
    "Bump kuzu to 0.11",
    "Cache repeated recall queries",
    "Handle empty snapshot gracefully",
    "Split serve() from CLI",
    "Add force-graph viewer",
    "Tighten 0700 dir perms",
    "Backfill learned_at on facts",
    "Debounce session events",
]

_FACT_TEMPLATES = [
    "{proj} depends on the shared ingest pipeline",
    "{repo} pins its runtime deps explicitly",
    "{person} owns review sign-off for {proj}",
    "{proj} runs a nightly reconciliation job",
    "The {repo} test suite must stay green before merge",
    "{person} flagged a race condition in {proj}",
    "{proj} redacts secrets before any persist",
    "{repo} is bound to loopback only for local dev",
]


def seed_demo(store: MemoryStore, seed: int = 1337) -> int:
    """Populate ``store`` with the demo graph. Returns total node count."""
    rng = random.Random(seed)
    nodes: list[Node] = []
    edges: list[Edge] = []

    project_ids: list[str] = []
    repo_ids: list[str] = []
    pr_ids: list[str] = []
    person_ids: list[str] = []

    for i, (name, status) in enumerate(_PROJECTS):
        pid = f"proj-{i}"
        project_ids.append(pid)
        nodes.append(Node("Project", pid, {"name": name, "status": status}))

    for i, name in enumerate(_PEOPLE):
        person_id = f"person-{i}"
        person_ids.append(person_id)
        nodes.append(Node("Person", person_id, {"name": name}))

    # Each project owns 2-3 repos.
    for pid in project_ids:
        for _ in range(rng.randint(2, 3)):
            rid = f"repo-{len(repo_ids)}"
            repo_ids.append(rid)
            nodes.append(
                Node("Repo", rid, {"path": f"~/gitstuff/{rid}", "remote": f"git@internal/{rid}"})
            )
            edges.append(Edge("owns", "Project", pid, "Repo", rid))

    # Each repo has several PRs; each PR is authored by one and reviewed by 1-2.
    for rid in repo_ids:
        for _ in range(rng.randint(3, 6)):
            pr_num = len(pr_ids)
            pr_id = f"pr-{pr_num}"
            pr_ids.append(pr_id)
            title = rng.choice(_PR_TITLES)
            state = rng.choice(["open", "merged", "closed"])
            nodes.append(
                Node("PR", pr_id, {"title": title, "number": pr_num + 1, "state": state})
            )
            edges.append(Edge("owns", "Repo", rid, "PR", pr_id))
            author = rng.choice(person_ids)
            edges.append(Edge("authored_by", "PR", pr_id, "Person", author))
            reviewers = rng.sample(person_ids, rng.randint(1, 2))
            for reviewer in reviewers:
                if reviewer != author:
                    edges.append(Edge("reviewed_by", "PR", pr_id, "Person", reviewer))

    # Facts relate to a random project, repo, person, or PR.
    project_names = {
        pid: name for pid, (name, _) in zip(project_ids, _PROJECTS, strict=True)
    }
    person_names = dict(zip(person_ids, _PEOPLE, strict=True))
    for i in range(40):
        fid = f"fact-{i}"
        template = rng.choice(_FACT_TEMPLATES)
        pid = rng.choice(project_ids)
        rid = rng.choice(repo_ids)
        person_id = rng.choice(person_ids)
        text = template.format(
            proj=project_names[pid], repo=rid, person=person_names[person_id]
        )
        nodes.append(Node("Fact", fid, {"text": text, "learned_at": "2026-07-07"}))
        target = rng.choice(
            [("Project", pid), ("Repo", rid), ("Person", person_id), ("PR", rng.choice(pr_ids))]
        )
        edges.append(Edge("relates_to", "Fact", fid, target[0], target[1]))

    store.remember(nodes=nodes, edges=edges)
    return len(nodes)
