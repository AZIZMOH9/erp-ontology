"""Merge duplicate concepts after a parallel run.

Sequential mapping avoids duplication at the source: each cluster is told what earlier clusters
named. Parallel mapping cannot -- workers run at the same time and never see each other -- so the
duplication is repaired afterwards instead.

Two stages, cheap before expensive:

1. **Deterministic.** Labels that normalise to the same canonical concept through the Phase 0
   vocabulary (``Customer`` / ``Client`` / ``BusinessPartner``) are merged for free.
2. **Semantic.** One model call over the remaining label list -- names only, never schemas, so it
   is a rounding error on the cost of the run -- catches the pairs no synonym list anticipated
   (``ScrapEvent`` / ``ProductionWaste``).

The duplication rate is reported either way. It is the spec's acceptance test for merge quality,
and a sequential run should be measured against it too.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from pydantic import BaseModel, Field

from erp_planner.llm.prompts import RECONCILE_SYSTEM  # noqa: F401
from erp_planner.models import Ontology
from erp_planner.vocabulary import Vocabulary, default_vocabulary, normalise


class ConceptGroup(BaseModel):
    canonical: str = Field(description="The clearest label, chosen from the group.")
    aliases: list[str] = Field(description="The other labels meaning the same concept.")
    reason: str = Field(description="One short sentence.")


class Reconciliation(BaseModel):
    groups: list[ConceptGroup]


class ReconcileReport(BaseModel):
    proposed: int = 0
    final: int = 0
    merged_by_vocabulary: int = 0
    merged_by_model: int = 0
    renames: dict[str, str] = Field(default_factory=dict)

    @property
    def duplication_rate(self) -> float:
        """Fraction of proposed concept names that turned out to be duplicates."""
        return (self.proposed - self.final) / self.proposed if self.proposed else 0.0


def deterministic_renames(
    labels: list[str], vocabulary: Vocabulary | None = None
) -> dict[str, str]:
    """Merge labels that resolve to the same canonical concept. Free, no API call.

    Which label survives is decided in this order:

    1. the one that *is* the vocabulary's canonical name (``Customer`` beats ``Client``),
    2. the one used by the most tables,
    3. alphabetical.

    Order matters more than it looks. Picking alphabetically alone would rename ``Customer`` to
    ``Client`` across the whole ontology, which is a correct merge with an unrecognisable result.
    None of the three rules depends on the order clusters finished in, so a parallel run merges
    the same way every time.
    """
    vocab = vocabulary or default_vocabulary()
    counts = Counter(labels)
    groups: dict[str, list[str]] = defaultdict(list)
    for label in sorted(set(labels)):
        groups[vocab.canonical(label)].append(label)

    renames: dict[str, str] = {}
    for canonical, members in groups.items():
        if len(members) < 2:
            continue
        winner = min(
            members,
            key=lambda label: (normalise(label) != canonical, -counts[label], label),
        )
        for member in members:
            if member != winner:
                renames[member] = winner
    return renames


def apply_renames(ontology: Ontology, renames: dict[str, str]) -> None:
    """Rewrite class labels and any is-a parents that referenced a merged name."""
    for mapping in ontology.classes:
        mapping.label = renames.get(mapping.label, mapping.label)
        if mapping.parent:
            mapping.parent = renames.get(mapping.parent, mapping.parent)


def reconcile(
    ontology: Ontology,
    call_model=None,
    vocabulary: Vocabulary | None = None,
) -> ReconcileReport:
    """Collapse duplicate concepts in ``ontology``, in place.

    ``call_model`` takes the rendered label list and returns a :class:`Reconciliation`. Pass None
    to run the deterministic stage only.
    """
    labels = [c.label for c in ontology.classes]
    report = ReconcileReport(proposed=len(set(labels)))

    renames = deterministic_renames(labels, vocabulary)
    report.merged_by_vocabulary = len(renames)
    apply_renames(ontology, renames)

    if call_model is not None:
        remaining = sorted({c.label: c.table for c in ontology.classes}.items())
        if len(remaining) > 1:
            listing = "\n".join(f"{label}  (from {table})" for label, table in remaining)
            result = call_model(listing)
            known = {label for label, _ in remaining}
            model_renames: dict[str, str] = {}
            for group in result.groups:
                if group.canonical not in known:
                    continue  # the model invented a name; ignore rather than rewrite to fiction
                for alias in group.aliases:
                    if alias in known and alias != group.canonical:
                        model_renames[alias] = group.canonical
            report.merged_by_model = len(model_renames)
            apply_renames(ontology, model_renames)
            renames.update(model_renames)

    report.renames = renames
    report.final = len({c.label for c in ontology.classes})
    return report
