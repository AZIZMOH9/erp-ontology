"""Rendering a schema fragment for a language model.

The instructions themselves live in :mod:`erp_planner.prompts`; this builds the schema text they
wrap around, and assembles the cached prefix."""

from __future__ import annotations

from erp_planner.clustering import Cluster
from erp_planner.llm.prompts import MAPPING_EXAMPLES, MAPPING_SYSTEM
from erp_planner.models import SchemaSnapshot, Table, Tier
from erp_planner.vocabulary import Vocabulary, default_vocabulary


def _render_column(table: Table, column, width: int = 24) -> str:
    bits = [f"    {column.name:<{width}} {column.data_type.lower()}"]
    if column.is_primary_key:
        bits.append("PK")
    if column.custom:
        bits.append("[CUSTOM FIELD]")
    if column.description:
        bits.append(f'"{column.description}"')
    if column.distinct_count is not None:
        bits.append(f"distinct={column.distinct_count}")
    if column.null_fraction is not None and column.null_fraction > 0.05:
        bits.append(f"null={column.null_fraction:.0%}")
    if column.sample_values:
        bits.append("e.g. " + ", ".join(column.sample_values[:4]))
    return "  ".join(bits)


def render_table(table: Table) -> str:
    """Full detail: what the model needs to decide what a table means."""
    header = f"TABLE {table.name}"
    facts = [f"tier={table.tier.value}"]
    if table.row_count is not None:
        facts.append(f"rows~{table.row_count}")
    lines = [f"{header}  ({', '.join(facts)})"]
    if table.description:
        lines.append(f"  described by the ERP as: {table.description}")
    lines.append("  columns:")
    width = max((len(c.name) for c in table.columns), default=8)
    lines.extend(_render_column(table, c, width) for c in table.columns)
    if table.foreign_keys:
        lines.append("  foreign keys:")
        lines.extend(
            f"    {', '.join(fk.from_columns)} -> {fk.to_table}.{', '.join(fk.to_columns)}"
            for fk in table.foreign_keys
        )
    return "\n".join(lines)


# How much of a context table to show. It is a reminder of what a key points at, not a second
# table to map, so a dozen column names is enough and more is paid for on every call.
CONTEXT_COLUMNS = 12

# Customisations listed in the cached prefix. Enough to orient the model; the cluster itself
# carries the detail.
OVERVIEW_CUSTOM = 20


def render_context_table(table: Table) -> str:
    """Abbreviated: enough to know what a key points at, not enough to spend tokens on."""
    described = f" — {table.description}" if table.description else ""
    columns = ", ".join(c.name for c in table.columns[:CONTEXT_COLUMNS])
    return f"TABLE {table.name}{described}\n    columns: {columns}"


def render_registry(concepts: dict[str, str], limit: int = 120) -> str:
    """Concepts established by earlier clusters, so later ones reuse instead of reinvent."""
    if not concepts:
        return ""
    lines = [f"  {label} (from {source})" for label, source in list(concepts.items())[:limit]]
    return (
        "ALREADY ESTABLISHED CONCEPTS — reuse these names when a table means the same thing:\n"
        + "\n".join(lines)
    )


def build_user_message(cluster: Cluster, concepts: dict[str, str]) -> str:
    sections = []
    registry = render_registry(concepts)
    if registry:
        sections.append(registry)
    if cluster.context:
        sections.append(
            "CONTEXT — referenced by the tables below, already mapped elsewhere. "
            "Do NOT map these:\n\n"
            + "\n\n".join(render_context_table(t) for t in cluster.context)
        )
    sections.append(
        "MAP THESE TABLES:\n\n" + "\n\n".join(render_table(t) for t in cluster.tables)
    )
    return "\n\n".join(sections)


# --------------------------------------------------------------------------------------
# The cached prefix
# --------------------------------------------------------------------------------------

# Worked examples. These are the single cheapest accuracy lever available and they cost nothing
# per call once the prefix caches, so they live in the prefix rather than in any one message.


def render_overview(snapshot: SchemaSnapshot) -> str:
    """A stable summary of the database. Identical for every cluster in a run, so it caches."""
    tiers: dict[str, int] = {}
    for table in snapshot.tables:
        tiers[table.tier.value] = tiers.get(table.tier.value, 0) + 1
    custom_tables = [t.name for t in snapshot.tables if t.tier is Tier.CUSTOM][:OVERVIEW_CUSTOM]
    custom_columns = [
        f"{t.name}.{c.name}" for t in snapshot.tables for c in t.columns if c.custom
    ][:OVERVIEW_CUSTOM]

    lines = [
        "THIS DATABASE",
        f"  ERP: {snapshot.source} {snapshot.source_version or ''}".rstrip(),
        f"  {len(snapshot.tables)} tables, "
        f"{sum(len(t.columns) for t in snapshot.tables)} columns, "
        f"{sum(len(t.foreign_keys) for t in snapshot.tables)} foreign keys",
        f"  tiers: {tiers}",
        f"  sample values masked as: {snapshot.masking or 'not masked'}",
    ]
    if custom_tables:
        lines.append(f"  customer-built tables: {', '.join(custom_tables)}")
    if custom_columns:
        lines.append(f"  customer-built fields: {', '.join(custom_columns)}")
    return "\n".join(lines)


def render_vocabulary(vocabulary: Vocabulary | None = None, limit: int = 60) -> str:
    """The concept names to prefer. Consistency across clusters is worth more than novelty."""
    vocab = vocabulary or default_vocabulary()
    names = sorted(vocab.groups)[:limit]
    if not names:
        return ""
    return (
        "PREFERRED CONCEPT NAMES — use one of these when it fits, so the ontology stays "
        "internally consistent. Coin a new name only when nothing here means the same thing:\n  "
        + ", ".join(names)
    )


def build_prefix(snapshot: SchemaSnapshot, vocabulary: Vocabulary | None = None) -> str:
    """The stable prefix, byte-identical on every call in a run.

    Nothing cluster-specific may go in here: a single differing byte invalidates the cache for
    every call after it, and the prefix is the only part of the prompt that can be cached at all.
    """
    parts = [MAPPING_SYSTEM, render_overview(snapshot), render_vocabulary(vocabulary), MAPPING_EXAMPLES]
    return "\n\n".join(p for p in parts if p)
