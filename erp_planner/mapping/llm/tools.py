"""Tools the worker agent can call on the agent path.

Four, in ascending order of power and risk:

``fetch_sample_values``   more values from one column, masked as configured
``walk_foreign_keys``     what a table points at, and what points at it, beyond its cluster
``search_concepts``       concepts already established, by name -- works where a static registry
                          cannot, e.g. in parallel mode
``column_statistics``     a read-only aggregate over one column, to test a hypothesis

The last one is the only tool that reaches the database with a question the agent composed, and it
is deliberately **not** a SQL tool. The agent picks a table, a column and an operation from a fixed
set; this module writes the SQL. An agent that can compose SQL against a production ERP is a
different risk conversation, and the aggregates below answer the questions that actually come up
("is this a small enum or a free-text field?") without opening it.

Every call is recorded in a :class:`ToolLog`, so an agent run can be read back afterwards.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from erp_planner.masking import MaskingMode, mask_value
from erp_planner.models import SchemaSnapshot

MAX_SAMPLE_VALUES = 40
MAX_NEIGHBOURS = 25


class Operation(StrEnum):
    """The questions the agent is allowed to ask the database."""

    DISTINCT_COUNT = "distinct_count"
    VALUE_FREQUENCY = "value_frequency"  # top values and their counts
    MIN_MAX = "min_max"
    NULL_FRACTION = "null_fraction"


class ToolCall(BaseModel):
    tool: str
    arguments: dict[str, str | int]
    ok: bool = True
    detail: str = ""


class ToolLog(BaseModel):
    calls: list[ToolCall] = Field(default_factory=list)

    def record(self, tool: str, arguments: dict, ok: bool = True, detail: str = "") -> None:
        self.calls.append(
            ToolCall(tool=tool, arguments={k: v for k, v in arguments.items()}, ok=ok, detail=detail)
        )

    def __len__(self) -> int:
        return len(self.calls)


class SchemaTools:
    """The tool implementations, bound to one snapshot and (optionally) one live database.

    Without an engine the database-backed tools decline politely rather than failing: the agent
    path still works on a snapshot alone, just with less evidence.
    """

    def __init__(
        self,
        snapshot: SchemaSnapshot,
        engine: Engine | None = None,
        masking: MaskingMode = MaskingMode.ALL,
        schema: str = "public",
        log: ToolLog | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.engine = engine
        self.masking = masking
        self.schema = schema
        # `log or ToolLog()` would discard a shared empty log: ToolLog defines __len__, so an
        # empty one is falsy. Every agent run would then write to a private log and the run's
        # tool history would come back empty.
        self.log = log if log is not None else ToolLog()
        self._concepts: dict[str, str] = {}

    # -- registry ------------------------------------------------------------------------
    def set_concepts(self, concepts: dict[str, str]) -> None:
        self._concepts = dict(concepts)

    def search_concepts(self, query: str, limit: int = 10) -> str:
        """Concepts already established elsewhere in this run, matched on substring."""
        needle = query.lower().strip()
        hits = [
            f"{label} (from {table})"
            for label, table in self._concepts.items()
            if needle in label.lower() or needle in table.lower()
        ][:limit]
        self.log.record("search_concepts", {"query": query}, detail=f"{len(hits)} hits")
        if not hits:
            return f"No established concept matches {query!r}. {len(self._concepts)} concepts known."
        return "\n".join(hits)

    # -- structure -----------------------------------------------------------------------
    def walk_foreign_keys(self, table: str) -> str:
        """What ``table`` points at, and what points at it."""
        target = self.snapshot.table(table)
        if target is None:
            self.log.record("walk_foreign_keys", {"table": table}, ok=False, detail="unknown table")
            return f"No table named {table!r} in this schema."

        out = [f"{table} references:"]
        for fk in target.foreign_keys:
            referenced = self.snapshot.table(fk.to_table)
            described = f" — {referenced.description}" if referenced and referenced.description else ""
            out.append(f"  {', '.join(fk.from_columns)} -> {fk.to_table}{described}")
        if len(out) == 1:
            out.append("  (nothing)")

        inbound = [
            f"  {other.name}.{', '.join(fk.from_columns)}"
            for other in self.snapshot.tables
            for fk in other.foreign_keys
            if fk.to_table == table and other.name != table
        ][:MAX_NEIGHBOURS]
        out.append(f"referenced by ({len(inbound)} shown):")
        out.extend(inbound or ["  (nothing)"])
        self.log.record("walk_foreign_keys", {"table": table}, detail=f"{len(inbound)} inbound")
        return "\n".join(out)

    # -- data ----------------------------------------------------------------------------
    def fetch_sample_values(self, table: str, column: str, limit: int = 20) -> str:
        """More distinct values from one column. Masked exactly as ingestion was configured."""
        limit = max(1, min(limit, MAX_SAMPLE_VALUES))
        args = {"table": table, "column": column, "limit": limit}
        if self.engine is None:
            cached = self._from_snapshot(table, column)
            self.log.record("fetch_sample_values", args, ok=cached is not None, detail="snapshot")
            return cached or f"No live database; the snapshot holds no samples for {table}.{column}."

        if not self._column_exists(table, column):
            self.log.record("fetch_sample_values", args, ok=False, detail="unknown column")
            return f"No column {table}.{column} in this schema."

        sql = text(
            f'SELECT DISTINCT "{column}" AS v FROM "{self.schema}"."{table}" '
            f'WHERE "{column}" IS NOT NULL LIMIT :limit'
        )
        try:
            with self.engine.connect() as conn:
                values = [str(r.v) for r in conn.execute(sql, {"limit": limit})]
        except Exception as exc:  # a tool failure must read as a tool result, not crash the run
            self.log.record("fetch_sample_values", args, ok=False, detail=type(exc).__name__)
            return f"Query failed: {type(exc).__name__}"

        if self.masking is not MaskingMode.NONE:
            values = [mask_value(v) for v in values]
        self.log.record("fetch_sample_values", args, detail=f"{len(values)} values")
        return f"{len(values)} distinct values of {table}.{column}:\n" + "\n".join(values)

    def column_statistics(self, table: str, column: str, operation: Operation) -> str:
        """One aggregate over one column. The SQL is written here, never by the agent."""
        args = {"table": table, "column": column, "operation": str(operation)}
        if self.engine is None:
            self.log.record("column_statistics", args, ok=False, detail="no engine")
            return "No live database is connected; statistics are unavailable."
        if not self._column_exists(table, column):
            self.log.record("column_statistics", args, ok=False, detail="unknown column")
            return f"No column {table}.{column} in this schema."

        op = Operation(operation)
        qualified = f'"{self.schema}"."{table}"'
        col = f'"{column}"'
        statements = {
            Operation.DISTINCT_COUNT: f"SELECT COUNT(DISTINCT {col}) AS a, COUNT(*) AS b FROM {qualified}",
            Operation.NULL_FRACTION: (
                f"SELECT COUNT(*) FILTER (WHERE {col} IS NULL) AS a, COUNT(*) AS b FROM {qualified}"
            ),
            Operation.MIN_MAX: f"SELECT MIN({col})::text AS a, MAX({col})::text AS b FROM {qualified}",
            Operation.VALUE_FREQUENCY: (
                f"SELECT {col}::text AS a, COUNT(*) AS b FROM {qualified} "
                f"WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 15"
            ),
        }
        try:
            with self.engine.connect() as conn:
                rows = list(conn.execute(text(statements[op])))
        except Exception as exc:
            self.log.record("column_statistics", args, ok=False, detail=type(exc).__name__)
            return f"Query failed: {type(exc).__name__}"

        self.log.record("column_statistics", args, detail=f"{len(rows)} rows")
        if op is Operation.DISTINCT_COUNT:
            return f"{table}.{column}: {rows[0].a} distinct values over {rows[0].b} rows."
        if op is Operation.NULL_FRACTION:
            total = rows[0].b or 1
            return f"{table}.{column}: {rows[0].a} of {rows[0].b} rows are null ({rows[0].a / total:.1%})."
        if op is Operation.MIN_MAX:
            lo, hi = rows[0].a, rows[0].b
            if self.masking is not MaskingMode.NONE:
                lo, hi = mask_value(str(lo or "")), mask_value(str(hi or ""))
            return f"{table}.{column}: min={lo} max={hi}"
        lines = []
        for row in rows:
            value = mask_value(str(row.a)) if self.masking is not MaskingMode.NONE else row.a
            lines.append(f"  {value}  ×{row.b}")
        return f"{table}.{column} most frequent values:\n" + "\n".join(lines)

    # -- helpers -------------------------------------------------------------------------
    def _column_exists(self, table: str, column: str) -> bool:
        found = self.snapshot.table(table)
        return bool(found and found.column(column))

    def _from_snapshot(self, table: str, column: str) -> str | None:
        found = self.snapshot.table(table)
        col = found.column(column) if found else None
        if not col or not col.sample_values:
            return None
        return f"From the snapshot, {table}.{column}:\n" + "\n".join(col.sample_values)
