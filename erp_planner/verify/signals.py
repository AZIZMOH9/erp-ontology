"""Independent evidence that a mapping is right.

The first measured run settled what this module must not rely on: across 70 mappings, the model's
own confidence was *inverted* -- 94% correct below 0.90, 89% correct at or above it. An LLM
grading its own answer shares the blind spots that produced the answer, so every signal here is
computed from the schema, the ontology's own shape, or an independent run. The model's number is
carried along as one weak input, never as the verdict.

Each signal returns a **trust** in 0..1 and says why, so a flag can be explained to the reviewer
who has to act on it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from erp_planner.models import (
    ClassMapping,
    MappingKind,
    Ontology,
    PropertyMapping,
    SchemaSnapshot,
)
from erp_planner.naming import words_of
from erp_planner.vocabulary import Vocabulary, default_vocabulary


class Signal(BaseModel):
    name: str
    trust: float  # 0 = contradicted, 0.5 = no evidence either way, 1 = corroborated
    reason: str = ""

    @property
    def informative(self) -> bool:
        return abs(self.trust - 0.5) > 1e-9


# --------------------------------------------------------------------------------------
# 1 - datatype agreement (properties, deterministic)
# --------------------------------------------------------------------------------------

_XSD_FAMILIES = {
    "xsd:string": {"char", "text", "varchar", "uuid", "enum", "json"},
    "xsd:decimal": {"numeric", "decimal", "double", "real", "float", "money"},
    "xsd:integer": {"int", "serial", "bigint", "smallint"},
    "xsd:boolean": {"bool"},
    "xsd:date": {"date"},
    "xsd:dateTime": {"timestamp", "datetime"},
}


def datatype_agreement(mapping: PropertyMapping, snapshot: SchemaSnapshot) -> Signal:
    """Does the declared xsd type match the column's actual SQL type?

    A mismatch does not prove the *meaning* is wrong, but it proves the model was not reading the
    column carefully, which is the same thing a reviewer wants to know.
    """
    table = snapshot.table(mapping.table)
    column = table.column(mapping.column) if table else None
    if column is None or not mapping.datatype:
        return Signal(name="datatype", trust=0.5, reason="column or datatype missing")

    sql = column.data_type.lower()
    expected = _XSD_FAMILIES.get(mapping.datatype)
    if expected is None:
        return Signal(name="datatype", trust=0.5, reason=f"unknown xsd type {mapping.datatype}")
    if any(token in sql for token in expected):
        return Signal(name="datatype", trust=1.0, reason=f"{mapping.datatype} fits {sql}")
    return Signal(
        name="datatype", trust=0.0, reason=f"{mapping.datatype} does not fit column type {sql}"
    )


# --------------------------------------------------------------------------------------
# 2 - structural role coherence (classes, deterministic)
# --------------------------------------------------------------------------------------


class StructuralRole(StrEnum):
    MASTER = "master"  # referenced by many, references few
    TRANSACTION = "transaction"  # references many, referenced by few
    LINE = "line"  # references a transaction, referenced by nothing
    UNKNOWN = "unknown"


# Words that place a concept in a role. Deliberately short: a wrong guess here costs a false
# flag, and a missing word costs nothing but silence.
MASTER_WORDS = frozenset(
    {"customer","supplier","vendor","party","person","organisation","organization","product","item",
     "material","country","currency","user","employee","account","category","unit","measure",
     "warehouse","location","company","tag","type","tax","address","contact","partner"}
)
TRANSACTION_WORDS = frozenset(
    {"order","invoice","bill","payment","shipment","delivery","movement","event","entry","audit",
     "quotation","receipt","transaction","adjustment","booking","posting"}
)
LINE_WORDS = frozenset({"line", "item", "detail", "position", "row"})


def _inbound_counts(snapshot: SchemaSnapshot) -> dict[str, int]:
    counts = {t.name: 0 for t in snapshot.tables}
    for table in snapshot.tables:
        for fk in table.foreign_keys:
            if fk.to_table != table.name and fk.to_table in counts:
                counts[fk.to_table] += 1
    return counts


# Referenced by at least this many tables to count as master data. Scaled to schema size, because
# on a small schema every count is small and a fixed threshold calls nothing a master.
MASTER_SHARE = 0.25
MASTER_MINIMUM = 2


def structural_role(table_name: str, snapshot: SchemaSnapshot) -> StructuralRole:
    """What the foreign-key graph says this table is, independent of any label.

    Three shapes the graph can actually distinguish:

    * **master** -- many tables point at it. Customers, products, countries.
    * **line** -- it points at something that nothing else points at, i.e. at an event. An order
      line belongs to an order.
    * **transaction** -- nothing points at it and it points only at masters. It stands alone.

    The line/transaction split is the delicate one: an earlier version classified anything with a
    single foreign key as a line, which made ``sale_order`` a line, and paired with a
    "lines live in transactions" compatibility rule it silently excused every mismatch.
    """
    table = snapshot.table(table_name)
    if table is None:
        return StructuralRole.UNKNOWN
    inbound = _inbound_counts(snapshot)
    threshold = max(MASTER_MINIMUM, int(len(snapshot.tables) * MASTER_SHARE))
    if inbound.get(table_name, 0) >= threshold:
        return StructuralRole.MASTER
    if not table.foreign_keys:
        return StructuralRole.UNKNOWN
    # Does it belong to something that is itself an event?
    if any(inbound.get(fk.to_table, 1) == 0 for fk in table.foreign_keys):
        return StructuralRole.LINE
    return StructuralRole.TRANSACTION


def semantic_role(label: str) -> StructuralRole:
    """What the chosen label claims this is."""
    words = set(words_of(label))
    if words & LINE_WORDS:
        return StructuralRole.LINE
    if words & TRANSACTION_WORDS:
        return StructuralRole.TRANSACTION
    if words & MASTER_WORDS:
        return StructuralRole.MASTER
    return StructuralRole.UNKNOWN


# Nothing is excused. A label claiming "line" on a table that belongs to no event is a real
# disagreement with the data, and it is exactly the shape of the one class error the obfuscated
# run made. An earlier compatibility rule here silently forgave it.
COMPATIBLE: set[tuple[StructuralRole, StructuralRole]] = set()


def role_coherence(mapping: ClassMapping, snapshot: SchemaSnapshot) -> Signal:
    """Does the concept sit where the foreign-key graph says it should?

    A table nothing references, pointing at two masters, is a transaction. Calling it a
    ``TimesheetLine`` is not impossible, but it disagrees with the shape of the data.
    """
    structural = structural_role(mapping.table, snapshot)
    semantic = semantic_role(mapping.label)
    if structural is StructuralRole.UNKNOWN or semantic is StructuralRole.UNKNOWN:
        return Signal(name="role", trust=0.5, reason=f"structure={structural}, label={semantic}")
    if structural is semantic or (structural, semantic) in COMPATIBLE:
        return Signal(name="role", trust=1.0, reason=f"both read as {structural}")
    return Signal(
        name="role",
        trust=0.0,
        reason=f"foreign keys read as {structural}, but '{mapping.label}' names a {semantic}",
    )


# --------------------------------------------------------------------------------------
# 3 - vocabulary anchoring (classes, deterministic)
# --------------------------------------------------------------------------------------


def vocabulary_anchor(mapping: ClassMapping, vocabulary: Vocabulary | None = None) -> Signal:
    """Is this a business concept anyone else would recognise?

    An invented concept is not wrong -- Tier 2 tables genuinely need new names -- but it is
    unanchored, and unanchored concepts are where the review time should go.
    """
    vocab = vocabulary or default_vocabulary()
    canonical = vocab.canonical(mapping.label)
    known = {vocab.canonical(name) for name in vocab.groups}
    if canonical in known:
        return Signal(name="vocabulary", trust=1.0, reason=f"known concept ({canonical})")
    return Signal(name="vocabulary", trust=0.35, reason="concept not in the target vocabulary")


# --------------------------------------------------------------------------------------
# 4 - independent agreement (any kind) - the spec's double-mapping signal
# --------------------------------------------------------------------------------------


def agreement(
    mapping,
    kind: MappingKind,
    others: list[Ontology],
    vocabulary: Vocabulary | None = None,
) -> Signal:
    """Do independent runs, which could not see each other, reach the same conclusion?

    The strongest signal available, and the only one that catches a mapping that is wrong in a way
    the schema cannot reveal. Two runs agreeing is not proof -- they can share a misconception --
    but two runs disagreeing is near-proof that at least one is wrong.
    """
    vocab = vocabulary or default_vocabulary()
    votes = []
    for other in others:
        match = next((m for m in other.mappings(kind) if m.key == mapping.key), None)
        if match is not None:
            votes.append(vocab.equivalent(match.label, mapping.label))
    if not votes:
        return Signal(name="agreement", trust=0.5, reason="no independent run to compare")
    share = sum(votes) / len(votes)
    if share == 1.0:
        return Signal(name="agreement", trust=1.0, reason=f"{len(votes)} independent run(s) agree")
    if share == 0.0:
        return Signal(
            name="agreement", trust=0.0, reason=f"{len(votes)} independent run(s) disagree"
        )
    return Signal(name="agreement", trust=share, reason=f"{share:.0%} of runs agree")


# --------------------------------------------------------------------------------------
# 5 - the model's own confidence, carried but distrusted
# --------------------------------------------------------------------------------------


def self_confidence(mapping) -> Signal:
    """The model's own number.

    Measured on the first scored runs: 94% correct below 0.90, 89% at or above. It is kept as an
    input because it costs nothing, and weighted near zero because that is what it earned.
    """
    if mapping.confidence is None:
        return Signal(name="self_confidence", trust=0.5, reason="not reported")
    return Signal(
        name="self_confidence", trust=float(mapping.confidence), reason="model's own estimate"
    )


class SignalWeights(BaseModel):
    """How much each signal counts. Tuning is expected; the defaults are argued, not fitted."""

    agreement: float = 0.45
    role: float = 0.20
    datatype: float = 0.20
    vocabulary: float = 0.10
    self_confidence: float = 0.05

    def of(self, name: str) -> float:
        return getattr(self, name, 0.0)


DEFAULT_WEIGHTS = SignalWeights()


class Verdict(BaseModel):
    kind: MappingKind
    key: str
    label: str
    trust: float
    flagged: bool = False
    signals: list[Signal] = Field(default_factory=list)

    def explain(self) -> str:
        return "; ".join(s.reason for s in self.signals if s.informative) or "no evidence"
