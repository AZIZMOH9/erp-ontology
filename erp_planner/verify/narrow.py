"""Narrow a review queue to the disagreements that are real.

Verification flags a mapping when two independent runs disagree about it. On a 418-table Odoo that
flagged 59% of mappings -- against a 20% ceiling -- which is not review, it is re-authoring.

But most of those disagreements are not disagreements about *meaning*. Measured on a sample of 400:
95% were the same meaning in different words -- `displaySequence` against `sequenceOrder`,
`enablesCsvImport` against `csvImportEnabled`. Only about 4% were two runs genuinely reading a
table differently.

So the judge is asked about each disagreement, and the ones that turn out to be wording are
dropped from the queue. What is left is what a person should actually look at.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from erp_planner.llm.runner import ModelRunner
from erp_planner.models import MappingKind, Ontology, SchemaSnapshot
from erp_planner.verify.judge import Equivalence, Pair, judge
from erp_planner.verify.verifier import VerificationReport
from erp_planner.vocabulary import Vocabulary, default_vocabulary


class NarrowReport(BaseModel):
    """What the judge made of the disagreements."""

    flagged_before: int = 0
    compared: int = 0
    same_meaning: int = 0
    different_meaning: int = 0
    unclear: int = 0
    flagged_after: int = 0
    dropped: list[str] = Field(default_factory=list)

    @property
    def wording_share(self) -> float:
        return self.same_meaning / self.compared if self.compared else 0.0


def disagreements(
    ontology: Ontology,
    other: Ontology,
    vocabulary: Vocabulary | None = None,
) -> list[Pair]:
    """Mappings both runs produced but labelled differently.

    Vocabulary-equivalent labels are not disagreements -- `Customer` and `Client` already agree,
    and paying a model to confirm it would be waste.
    """
    vocab = vocabulary or default_vocabulary()
    pairs: list[Pair] = []
    for kind in MappingKind:
        theirs = {m.key: m for m in other.mappings(kind)}
        for mine in ontology.mappings(kind):
            match = theirs.get(mine.key)
            if match is None or vocab.equivalent(mine.label, match.label):
                continue
            table = getattr(mine, "table", getattr(mine, "from_table", ""))
            pairs.append(
                Pair(
                    kind=kind,
                    key=f"{kind.value}:{mine.key}",
                    table=table,
                    column=getattr(mine, "column", None),
                    label_a=mine.label,
                    label_b=match.label,
                )
            )
    return pairs


def narrow(
    report: VerificationReport,
    ontology: Ontology,
    other: Ontology,
    snapshot: SchemaSnapshot,
    runner: ModelRunner,
    sample: int | None = None,
) -> NarrowReport:
    """Drop flagged mappings whose disagreement is only wording. Modifies ``report`` in place.

    ``sample`` judges a random subset and reports the share, without changing the queue -- useful
    for finding out what a full pass would cost before paying for it.
    """
    result = NarrowReport(flagged_before=len(report.flagged))
    pairs = disagreements(ontology, other)
    if sample and sample < len(pairs):
        import random

        pairs = random.Random(7).sample(pairs, sample)

    verdicts = judge(runner, pairs, snapshot) if pairs else {}
    result.compared = len(verdicts)

    wording = set()
    for key, verdict in verdicts.items():
        if verdict.verdict is Equivalence.SAME:
            result.same_meaning += 1
            wording.add(key)
        elif verdict.verdict is Equivalence.DIFFERENT:
            result.different_meaning += 1
        else:
            result.unclear += 1

    if sample is None:
        for entry in report.verdicts:
            if entry.flagged and f"{entry.kind.value}:{entry.key}" in wording:
                entry.flagged = False
                entry.signals.append(
                    type(entry.signals[0])(
                        name="judge", trust=1.0, reason="the two runs mean the same thing"
                    )
                )
                result.dropped.append(entry.key)
    result.flagged_after = len(report.flagged)
    return result
