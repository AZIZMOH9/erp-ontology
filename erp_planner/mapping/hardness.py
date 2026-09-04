"""How hard is this cluster? Decided in code, never by a model.

This is the single constraint that keeps the orchestrator cheap, reproducible and measurable. The
moment an LLM decides which path a cluster takes, every run costs more, no two runs agree, and the
"what did agency actually add?" comparison stops being possible.

**Scored per table, then the maximum wins.** Averaging over a cluster hides the thing that makes
it hard: cluster #0 is one customer-built table beside eleven documented product tables, and a mean
scores that 0.08 — routine — when the whole reason to spend an agent on it is that one table. A
cluster is as hard as the hardest table in it.

Four signals, each 0..1, combined by weight:

``custom``          fraction of tables and columns the customer built — nothing to retrieve
``undocumented``    fraction of tables the ERP has no description for
``isolation``       how little foreign-key context the cluster has — a table pointing at nothing
                    must be read from its values alone
``opacity``         how uninformative the identifiers are — short, abbreviated, vowel-less names

Weights and threshold are configuration. Tuning is expected; changing them is a config edit and a
re-run, not a code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from erp_planner.clustering import Cluster
from erp_planner.models import Table, Tier


@dataclass(frozen=True)
class HardnessWeights:
    custom: float = 0.40
    undocumented: float = 0.25
    isolation: float = 0.15
    opacity: float = 0.20

    def total(self) -> float:
        return self.custom + self.undocumented + self.isolation + self.opacity


DEFAULT_WEIGHTS = HardnessWeights()

# Set equal to the ``custom`` weight, and compared with ``>=``, so a fully customer-built table
# clears it on that signal alone -- by construction, not by luck. On the demo Odoo that routes the
# two Tier 2 clusters (0.528, 0.500) and nothing else; the next cluster down is 0.383, an
# undocumented many-to-many join table, which is the easiest kind of table in the database.
DEFAULT_THRESHOLD = 0.40


class Hardness(BaseModel):
    """The score, with its components, so a routing decision can be explained and argued with."""

    score: float
    custom: float
    undocumented: float
    isolation: float
    opacity: float
    # The table that drove the score -- the reason this cluster is routed the way it is.
    driver: str | None = None
    reasons: list[str] = Field(default_factory=list)

    def explain(self) -> str:
        return ", ".join(self.reasons) if self.reasons else "routine"


_VOWELS = re.compile(r"[aeiou]")
_WORD = re.compile(r"[a-z]+")


def _identifier_opacity(name: str) -> float:
    """0 = a readable word, 1 = abbreviation soup.

    ``partner_id`` scores low; ``x_nc_cnt`` scores high. Short vowel-less tokens are the signature
    of a name a human shortened and never wrote down.
    """
    words = _WORD.findall(name.lower())
    if not words:
        return 1.0
    opaque = 0
    for word in words:
        if word in {"id", "x", "z", "ref", "no", "nr"}:
            continue
        if len(word) <= 4 and not _VOWELS.search(word):
            opaque += 1  # cnt, scr, prt, tmpl
        elif len(word) <= 3:
            opaque += 1
    countable = [w for w in words if w not in {"id", "x", "z"}]
    return opaque / len(countable) if countable else 1.0


def _table_opacity(table: Table) -> float:
    names = [table.name] + [c.name for c in table.columns]
    return sum(_identifier_opacity(n) for n in names) / len(names)


def table_hardness(table: Table, weights: HardnessWeights = DEFAULT_WEIGHTS) -> Hardness:
    """Score one table."""
    columns = table.columns
    custom = (
        1.0
        if table.tier is Tier.CUSTOM
        else (sum(1 for c in columns if c.custom) / len(columns) if columns else 0.0)
    )
    undocumented = 0.0 if table.description else 1.0
    # Foreign keys, saturating at 2 -- beyond that, more keys stop adding context.
    isolation = max(0.0, 1.0 - min(len(table.foreign_keys) / 2, 1.0))
    opacity = _table_opacity(table)

    raw = (
        weights.custom * custom
        + weights.undocumented * undocumented
        + weights.isolation * isolation
        + weights.opacity * opacity
    )
    total = weights.total()
    return Hardness(
        score=round(raw / total if total else 0.0, 4),
        custom=round(custom, 4),
        undocumented=round(undocumented, 4),
        isolation=round(isolation, 4),
        opacity=round(opacity, 4),
    )


def score(
    cluster: Cluster,
    weights: HardnessWeights = DEFAULT_WEIGHTS,
) -> Hardness:
    """Score a cluster as its hardest table. Pure function: same input, same score, every run."""
    if not cluster.tables:
        return Hardness(score=0.0, custom=0.0, undocumented=0.0, isolation=0.0, opacity=0.0)

    scored = [(table_hardness(t, weights), t) for t in cluster.tables]
    hardness, table = max(scored, key=lambda pair: (pair[0].score, pair[1].name))

    reasons = []
    if hardness.custom >= 0.2:
        reasons.append(f"{table.name} {hardness.custom:.0%} customer-built")
    if hardness.undocumented >= 0.5:
        reasons.append(f"{table.name} undocumented")
    if hardness.isolation >= 0.5:
        reasons.append(f"{table.name} has no foreign-key context")
    if hardness.opacity >= 0.5:
        reasons.append(f"{table.name} has opaque identifiers")

    return hardness.model_copy(update={"reasons": reasons, "driver": table.name})


def takes_agent_path(
    cluster: Cluster,
    threshold: float = DEFAULT_THRESHOLD,
    weights: HardnessWeights = DEFAULT_WEIGHTS,
) -> tuple[bool, Hardness]:
    hardness = score(cluster, weights)
    return hardness.score >= threshold, hardness
