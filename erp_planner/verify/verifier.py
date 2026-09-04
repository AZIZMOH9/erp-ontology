"""Combine the signals into one trust score, and decide what a human should look at.

Three numbers come out of this, and the spec gates on all three:

``catch rate``    of the mappings that are actually wrong, how many did we flag
``flag precision``of the mappings we flagged, how many were actually wrong
``review load``   flagged / total -- a 95% catch rate achieved by flagging half the ontology is
                  not review, it is re-authoring
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from erp_planner.models import MappingKind, Ontology, PropertyMapping, SchemaSnapshot
from erp_planner.verify.signals import (
    DEFAULT_WEIGHTS,
    Signal,
    SignalWeights,
    Verdict,
    agreement,
    datatype_agreement,
    role_coherence,
    self_confidence,
    vocabulary_anchor,
)
from erp_planner.vocabulary import Vocabulary

# Below this, a mapping goes in front of a human. Config: raising it catches more and costs more.
DEFAULT_FLAG_THRESHOLD = 0.65


class VerificationReport(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)
    threshold: float = DEFAULT_FLAG_THRESHOLD

    @property
    def flagged(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.flagged]

    @property
    def review_load(self) -> float:
        return len(self.flagged) / len(self.verdicts) if self.verdicts else 0.0

    def queue(self) -> list[Verdict]:
        """Flagged mappings, least trusted first -- the order a reviewer should work in."""
        return sorted(self.flagged, key=lambda v: v.trust)


def verify(
    ontology: Ontology,
    snapshot: SchemaSnapshot,
    against: list[Ontology] | None = None,
    weights: SignalWeights = DEFAULT_WEIGHTS,
    threshold: float = DEFAULT_FLAG_THRESHOLD,
    vocabulary: Vocabulary | None = None,
) -> VerificationReport:
    """Score every mapping, flag the ones worth a human's time.

    ``against`` holds independent mappings of the same schema. It is optional because a customer's
    first run has nothing to compare with -- but it is the strongest signal, so a second run is
    worth its cost.
    """
    report = VerificationReport(threshold=threshold)
    others = against or []

    for kind in MappingKind:
        for mapping in ontology.mappings(kind):
            signals: list[Signal] = [
                agreement(mapping, kind, others, vocabulary),
                self_confidence(mapping),
            ]
            if kind is MappingKind.CLASS:
                signals.append(role_coherence(mapping, snapshot))
                signals.append(vocabulary_anchor(mapping, vocabulary))
            if kind is MappingKind.PROPERTY and isinstance(mapping, PropertyMapping):
                signals.append(datatype_agreement(mapping, snapshot))

            # Only signals that actually saw something get a vote; an uninformative signal must
            # not drag a mapping toward 0.5 just by existing.
            voting = [s for s in signals if s.informative]
            if voting:
                total = sum(weights.of(s.name) for s in voting) or 1.0
                trust = sum(s.trust * weights.of(s.name) for s in voting) / total
            else:
                trust = 0.5

            report.verdicts.append(
                Verdict(
                    kind=kind,
                    key=mapping.key,
                    label=mapping.label,
                    trust=round(trust, 4),
                    flagged=trust < threshold,
                    signals=signals,
                )
            )
    return report


# --------------------------------------------------------------------------------------
# Measuring the flags against a gold standard
# --------------------------------------------------------------------------------------


class FlagQuality(BaseModel):
    total: int = 0
    wrong: int = 0
    flagged: int = 0
    caught: int = 0  # flagged and actually wrong

    @property
    def catch_rate(self) -> float:
        """Of the wrong mappings, how many reached a human."""
        return self.caught / self.wrong if self.wrong else 0.0

    @property
    def flag_precision(self) -> float:
        """Of the flagged mappings, how many were worth flagging."""
        return self.caught / self.flagged if self.flagged else 0.0

    @property
    def review_load(self) -> float:
        return self.flagged / self.total if self.total else 0.0

    @property
    def silent_errors(self) -> int:
        """Wrong mappings that shipped unflagged. The number the safety story rests on."""
        return self.wrong - self.caught


def evaluate_flags(report: VerificationReport, wrong_keys: set[tuple[str, str]]) -> FlagQuality:
    """Score the flags against a known-wrong set, e.g. from `benchmark score`."""
    quality = FlagQuality(total=len(report.verdicts))
    for verdict in report.verdicts:
        is_wrong = (verdict.kind.value, verdict.key) in wrong_keys
        quality.wrong += is_wrong
        quality.flagged += verdict.flagged
        quality.caught += is_wrong and verdict.flagged
    return quality


def ranking_quality(report: VerificationReport, wrong_keys: set[tuple[str, str]]) -> float:
    """Probability that a wrong mapping is ranked below a correct one (AUC).

    0.5 is a coin flip. This is the number that decides whether a review queue is worth ordering,
    and it is how the trust score should be judged against the model's own confidence -- which
    measured *below* 0.5 on the first runs.
    """
    wrong = [v.trust for v in report.verdicts if (v.kind.value, v.key) in wrong_keys]
    right = [v.trust for v in report.verdicts if (v.kind.value, v.key) not in wrong_keys]
    if not wrong or not right:
        return 0.5
    wins = sum((w < r) + 0.5 * (w == r) for w in wrong for r in right)
    return wins / (len(wrong) * len(right))
