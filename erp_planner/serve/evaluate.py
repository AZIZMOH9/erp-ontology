"""Measure the query layer against a known-correct answer.

RODI ships a reference SQL query beside every question. Running both and comparing result sets is
the only way to score an answer rather than a label -- and it is the spec's Phase 5 criterion: a
fixed question set with verified answers, reported as compounded end-to-end correctness.

Comparison is on *values*, not on SQL text. Two correct queries can be written a hundred ways;
what matters is whether the rows agree.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

_FIELD = re.compile(r"^([a-zA-Z]+)=(.*)$")


class Question(BaseModel):
    """One RODI query pair: an English-ish name and the SQL that answers it."""

    id: str
    question: str
    reference_sql: str
    categories: str = ""
    aggregate: bool = False  # the question asks for a count, not a list


class Outcome(BaseModel):
    question: str
    matched: bool = False
    reason: str = ""
    generated_sql: str = ""
    expected: list[list[str]] = Field(default_factory=list)
    actual: list[list[str]] = Field(default_factory=list)


class QueryReport(BaseModel):
    outcomes: list[Outcome] = Field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return sum(o.matched for o in self.outcomes) / len(self.outcomes) if self.outcomes else 0.0

    @property
    def answered(self) -> int:
        return sum(1 for o in self.outcomes if o.actual)


def load_questions(scenario: Path) -> list[Question]:
    """Read a RODI scenario's query pairs into questions with reference answers."""
    questions = []
    for path in sorted((scenario / "queries").glob("*.qpair")):
        text_ = path.read_text(errors="replace").replace("\\n\\\n", " ").replace("\\n\\", " ")
        fields, key = {}, None
        for line in text_.splitlines():
            match = _FIELD.match(line)
            if match:
                key, value = match.group(1), match.group(2)
                fields[key] = value.strip()
            elif key:
                fields[key] += " " + line.strip()
        if not fields.get("sql"):
            continue
        # RODI names questions like "Q22 (Peoples' Surnames)" - the parenthetical is the topic.
        name = fields.get("name", path.stem)
        topic = (name.split("(", 1)[1].rstrip(")") if "(" in name else name).strip()
        # Whether a question wants a count or a list is in RODI's SPARQL, which is its statement
        # of the question. Taking it from there restores intent the terse label loses; taking it
        # from the reference SQL would be reading the answer.
        aggregate = bool(re.search(r"\bCOUNT\s*\(", fields.get("sparql", ""), re.I))
        asked = f"How many {topic}?" if aggregate else f"List the {topic}."
        questions.append(
            Question(
                id=path.stem, question=asked, aggregate=aggregate,
                reference_sql=fields["sql"].strip(), categories=fields.get("categories", ""),
            )
        )
    return questions


def run_reference(engine: Engine, sql: str, schema: str, limit: int = 200) -> list[list[str]]:
    """RODI's SQL is written unqualified against its own schema; run it with that search path."""
    with engine.connect() as conn:
        conn.execute(text(f'SET search_path = "{schema}", public'))
        cursor = conn.execute(text(sql.rstrip(";")))
        return [[str(v) for v in row] for row in cursor.fetchmany(limit)]


def same_answer(expected: list[list[str]], actual: list[list[str]]) -> tuple[bool, str]:
    """Do two result sets carry the same answer?

    Order-insensitive, and tolerant of extra columns: a query answering "which persons" correctly
    should not fail for returning a name alongside an id. What must match is the set of values in
    the column the reference returns.
    """
    if not expected and not actual:
        return True, "both empty"
    if not actual:
        return False, "no rows returned"
    if len(expected[0]) == 1 and len(expected) == 1:
        # A scalar answer - a count. Compare the number wherever it sits in the row.
        want = expected[0][0]
        return (want in actual[0], f"expected {want}, got {actual[0]}")
    want = {tuple(sorted(row)) for row in expected}
    got_values = {v for row in actual for v in row}
    covered = sum(1 for row in want if all(v in got_values for v in row))
    share = covered / len(want) if want else 0.0
    return share >= 0.95, f"{covered}/{len(want)} reference rows present"
