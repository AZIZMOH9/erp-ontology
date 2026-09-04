"""Phase 5 -- answering questions through the ontology.

The spec asks for a virtual knowledge graph with SPARQL translated to SQL. What a user needs is
narrower: ask a question in English, get the right answer out of the ERP, without the data moving.
So this generates SQL directly, grounded in the ontology, and skips the RDF layer entirely. If a
customer later needs SPARQL, the mappings to build it from are already there.

Two properties matter more than elegance here:

* **Read-only, enforced in code.** The statement is checked before it runs, not trusted because
  the prompt asked nicely. Phase 6 writes go through the ERP's own API; nothing here may write.
* **Measurable.** RODI's query pairs ship a reference SQL query per question, so a generated
  answer can be compared against a known-correct one rather than eyeballed.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from erp_planner.llm.prompts import QUERY_SYSTEM
from erp_planner.llm.runner import ModelRunner
from erp_planner.models import MappingKind, Ontology, SchemaSnapshot

# Anything that is not a read. Checked on the generated statement, not asked for in the prompt.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call|do|merge)\b", re.I
)


class GeneratedQuery(BaseModel):
    sql: str = Field(description="One SELECT statement, or empty if unanswerable.")
    unanswerable: str = Field(
        default="", description="If the semantic layer cannot answer it, why. Otherwise empty."
    )
    reasoning: str = Field(description="One sentence: which concepts you used.")


class QueryResult(BaseModel):
    question: str
    sql: str = ""
    unanswerable: str = ""
    rows: list[list[str]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    error: str = ""

    @property
    def answered(self) -> bool:
        return bool(self.rows) and not self.error


def is_read_only(sql: str) -> bool:
    """A statement may run only if it is a single SELECT and contains no write verb.

    Deliberately strict and deliberately not a parser: the cost of a false negative is a refused
    question, and the cost of a false positive is someone's database.
    """
    stripped = re.sub(r"--[^\n]*", " ", sql).strip().rstrip(";")
    if not stripped:
        return False
    if ";" in stripped:  # no statement chaining
        return False
    if not re.match(r"^\s*(select|with)\b", stripped, re.I):
        return False
    return not _FORBIDDEN.search(stripped)


def relevant_tables(question: str, ontology: Ontology, limit: int) -> list[str]:
    """Pick the concepts a question is plausibly about, plus what they point at.

    A real ERP ontology has hundreds of concepts and they will not fit in one prompt. Taking the
    first N is the wrong answer -- on the 411-concept Odoo ontology it hid 85% of the schema, and
    the layer answered "there is no Country table" about a database that has one. So concepts are
    ranked by word overlap with the question, and each survivor drags in its foreign-key
    neighbours, because a question about orders usually needs the customer too.
    """
    # The vocabulary normaliser, not a raw split: a question says "countries" and the concept is
    # "Country". Without singularising, the two never meet and the layer reports no such table.
    from erp_planner.vocabulary import normalise

    asked = set(normalise(question).split())
    scored = []
    for mapping in ontology.classes:
        # Match the physical table too - `res_country` carries the word the question used.
        terms = set(normalise(mapping.label).split()) | set(normalise(mapping.table).split())
        scored.append((len(asked & terms), mapping.table))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))

    chosen = [table for score, table in scored if score][:limit]
    neighbours = [
        r.to_table for r in ontology.relations if r.from_table in set(chosen)
    ]
    for table in neighbours:
        if table not in chosen and len(chosen) < limit:
            chosen.append(table)
    # Nothing matched the wording - fall back to the largest concepts rather than nothing.
    if not chosen:
        chosen = [table for _, table in scored[:limit]]
    return chosen


# How much of each concept to put in front of the query model. A question needs to know a table's
# fields exist, not every one of them, and this is paid for on every question asked.
LAYER_PROPERTIES = 25
LAYER_RELATIONS = 10


def render_semantic_layer(
    ontology: Ontology,
    snapshot: SchemaSnapshot,
    schema: str,
    limit: int = 60,
    question: str | None = None,
) -> str:
    """The ontology as context: business meaning first, physical names beside it."""
    class_of = {c.table: c.label for c in ontology.classes}
    if question is not None and len(class_of) > limit:
        keep = set(relevant_tables(question, ontology, limit))
        class_of = {t: label for t, label in class_of.items() if t in keep}
    by_table: dict[str, list[str]] = {}
    for prop in ontology.properties:
        by_table.setdefault(prop.table, []).append(f"{prop.label} = {prop.column}")
    rels: dict[str, list[str]] = {}
    for rel in ontology.relations:
        rels.setdefault(rel.from_table, []).append(
            f"{rel.label} -> {rel.to_table} (via {', '.join(rel.from_columns)})"
        )

    blocks = []
    for table, label in list(class_of.items())[:limit]:
        physical = snapshot.table(table)
        lines = [f'{label}  =  table "{schema}"."{table}"']
        if physical and physical.row_count is not None:
            lines[0] += f"  (~{physical.row_count} rows)"
        for prop in by_table.get(table, [])[:LAYER_PROPERTIES]:
            lines.append(f"    {prop}")
        for rel in rels.get(table, [])[:LAYER_RELATIONS]:
            lines.append(f"    {rel}")
        blocks.append("\n".join(lines))
    return "SEMANTIC LAYER — business concept = physical table\n\n" + "\n\n".join(blocks)


def ask(
    runner: ModelRunner,
    question: str,
    ontology: Ontology,
    snapshot: SchemaSnapshot,
    engine: Engine,
    schema: str = "public",
    max_rows: int = 50,
) -> QueryResult:
    """Answer one question. Generates SQL, refuses anything that is not a read, then runs it."""
    layer = render_semantic_layer(ontology, snapshot, schema, question=question)
    try:
        generated = runner.structured(QUERY_SYSTEM + "\n\n" + layer, f"Question: {question}", GeneratedQuery)
    except Exception as exc:
        return QueryResult(question=question, error=f"generation failed: {type(exc).__name__}")

    if generated.unanswerable:
        return QueryResult(question=question, unanswerable=generated.unanswerable)
    if not is_read_only(generated.sql):
        return QueryResult(
            question=question, sql=generated.sql, error="refused: not a single read-only statement"
        )

    try:
        with engine.connect() as conn:
            cursor = conn.execute(text(generated.sql.rstrip(";")))
            columns = list(cursor.keys())
            rows = [[str(v) for v in row] for row in cursor.fetchmany(max_rows)]
    except Exception as exc:
        return QueryResult(question=question, sql=generated.sql, error=f"{type(exc).__name__}: {exc}")
    return QueryResult(question=question, sql=generated.sql, columns=columns, rows=rows)


def mapping_counts(ontology: Ontology) -> dict[str, int]:
    return {kind.value: len(ontology.mappings(kind)) for kind in MappingKind}
