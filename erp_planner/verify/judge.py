"""Decide whether two labels mean the same thing, given the data.

Every measurement in this project has been distorted by one question nobody could answer at
scale: is a mismatch a different *meaning*, or the same meaning in different *words*? Answering it
by hand is slow and unreliable -- four consecutive judgements made from labels alone were
overturned the moment the underlying data was queried.

So the judge is given the evidence a reviewer would have: the real column, its type, its
cardinality and its actual values. `has_a_location` versus `city` is unanswerable from the labels
and obvious from three rows of `Mascara, Pago Pago, Tirane`.

Two things keep this from being a model grading itself:

* **It judges equivalence, not correctness.** A far narrower question than mapping, and one where
  the evidence usually settles it.
* **It is validated before it is trusted.** `data/benchmarks/rodi/judge-validation.json` holds
  hand-labelled verdicts, each checked against the database. A judge that cannot reproduce those
  is not used.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from erp_planner.llm.prompts import JUDGE_SYSTEM
from erp_planner.llm.runner import ModelRunner
from erp_planner.models import MappingKind, Ontology, SchemaSnapshot

# Columns shown when the judge is ruling on a whole table rather than one column. Enough to see
# what the table holds; every extra one is paid for on every batch.
EVIDENCE_COLUMNS = 12


class Equivalence(StrEnum):
    SAME = "same"
    DIFFERENT = "different"
    UNCLEAR = "unclear"


class ItemVerdict(BaseModel):
    key: str = Field(description="The item key, copied exactly from the input.")
    verdict: Equivalence
    reason: str = Field(description="One short sentence citing the evidence used.")


class JudgeBatch(BaseModel):
    verdicts: list[ItemVerdict]


class Pair(BaseModel):
    """One thing to judge."""

    kind: MappingKind
    key: str
    table: str
    column: str | None = None
    label_a: str
    label_b: str


def evidence_for(pair: Pair, snapshot: SchemaSnapshot) -> str:
    """What a reviewer would look at before deciding."""
    table = snapshot.table(pair.table)
    if table is None:
        return "  (table not found in the schema)"
    lines = []
    if pair.column:
        column = table.column(pair.column)
        if column:
            lines.append(f"  column {pair.table}.{pair.column}  type={column.data_type}")
            if column.distinct_count is not None:
                lines.append(f"  distinct values: {column.distinct_count} of ~{table.row_count} rows")
            if column.sample_values:
                lines.append("  values: " + ", ".join(column.sample_values[:6]))
            if column.description:
                lines.append(f"  the ERP calls it: {column.description}")
    else:
        lines.append(f"  table {pair.table}  rows~{table.row_count}")
        lines.append("  columns: " + ", ".join(c.name for c in table.columns[:EVIDENCE_COLUMNS]))
        if table.foreign_keys:
            lines.append(
                "  points at: "
                + ", ".join(f"{','.join(fk.from_columns)}->{fk.to_table}" for fk in table.foreign_keys[:6])
            )
        inbound = [
            o.name for o in snapshot.tables for fk in o.foreign_keys
            if fk.to_table == pair.table and o.name != pair.table
        ][:6]
        if inbound:
            lines.append("  referenced by: " + ", ".join(inbound))
    return "\n".join(lines) or "  (no evidence available)"


def render_batch(pairs: list[Pair], snapshot: SchemaSnapshot) -> str:
    blocks = []
    for pair in pairs:
        blocks.append(
            f"### {pair.key}\n"
            f"  label A: {pair.label_a}\n"
            f"  label B: {pair.label_b}\n"
            f"{evidence_for(pair, snapshot)}"
        )
    return (
        "For each item, do label A and label B describe the same thing?\n"
        "Return one verdict per item, copying the key exactly.\n\n" + "\n\n".join(blocks)
    )


def judge(
    runner: ModelRunner,
    pairs: list[Pair],
    snapshot: SchemaSnapshot,
    prefix: str = JUDGE_SYSTEM,
    batch_size: int = 10,
) -> dict[str, ItemVerdict]:
    """Judge every pair. Batched, because the judgement is short and the evidence is not."""
    out: dict[str, ItemVerdict] = {}
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start : start + batch_size]
        try:
            result = runner.structured(prefix, render_batch(batch, snapshot), JudgeBatch)
        except Exception as exc:  # a failed batch must not lose the run
            for pair in batch:
                out[pair.key] = ItemVerdict(
                    key=pair.key, verdict=Equivalence.UNCLEAR, reason=f"judge failed: {type(exc).__name__}"
                )
            continue
        for verdict in result.verdicts:
            if verdict.key in {p.key for p in batch}:
                out[verdict.key] = verdict
        for pair in batch:  # anything the model dropped
            out.setdefault(
                pair.key, ItemVerdict(key=pair.key, verdict=Equivalence.UNCLEAR, reason="not returned")
            )
    return out


def pairs_from(prediction: Ontology, gold: Ontology) -> list[Pair]:
    """Every mapping present in both, so the judge sees exactly what the scorer scored."""
    pairs: list[Pair] = []
    for kind in MappingKind:
        predicted = {m.key: m for m in prediction.mappings(kind)}
        for gold_mapping in gold.mappings(kind):
            match = predicted.get(gold_mapping.key)
            if match is None:
                continue
            table = getattr(gold_mapping, "table", getattr(gold_mapping, "from_table", ""))
            pairs.append(
                Pair(
                    kind=kind,
                    key=f"{kind.value}:{gold_mapping.key}",
                    table=table,
                    column=getattr(gold_mapping, "column", None),
                    label_a=gold_mapping.label,
                    label_b=match.label,
                )
            )
    return pairs
