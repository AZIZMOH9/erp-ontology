"""Split a schema into work units.

One table alone is often unreadable. ``x_sup_qual_rec`` with columns ``x_prt``, ``x_scr``,
``x_nc_cnt`` means very little; the same table shown next to ``res_partner`` and ``res_users``,
with the foreign keys drawn in, reads as a supplier quality audit. Context is most of the signal
on exactly the tables that matter, so work units are **foreign-key neighbourhoods**, not chunks.

Two things complicate that:

* **Bookkeeping edges.** Nearly every Odoo table points at ``res_users`` and ``res_company``
  through ``create_uid`` / ``write_uid`` / ``company_id``. Following those makes the whole database
  one component. The noise is in specific *columns*, not in the tables they point at, so those
  edges are cut while real business edges (``stock_move.product_id``) survive. A degree threshold
  remains as a backstop for anything the column list misses. Cut targets come back as *context*:
  shown to the model, but mapped once, in their own cluster.
* **Size.** A cluster has to fit in one request with room for the answer, so growth is capped by
  both table count and column count (columns drive the size of the output).
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field

from erp_planner.models import ForeignKey, SchemaSnapshot, Table, Tier

# Audit and multi-company columns every table carries. Their edges say "this row was written by
# a user at a company" -- true of everything, and therefore no evidence of what a table means.
BOOKKEEPING_COLUMNS = frozenset(
    {
        "create_uid",
        "write_uid",
        "company_id",
        "currency_id",
        "company_currency_id",
        "user_id",
        "partner_id_company",
    }
)

# Backstop for hubs the column list misses. Deliberately high: cutting a real business table out
# of the graph strands its neighbours as singletons, which costs one API call each and hands the
# model the least context exactly where it needs the most.
DEFAULT_HUB_THRESHOLD = 60


class Cluster(BaseModel):
    """One request's worth of schema."""

    index: int = 0
    tables: list[Table] = Field(default_factory=list)
    # Referenced from this cluster but mapped in another one. Shown abbreviated, for context only.
    context: list[Table] = Field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    @property
    def column_count(self) -> int:
        return sum(len(t.columns) for t in self.tables)

    @property
    def has_custom(self) -> bool:
        return any(t.tier is Tier.CUSTOM for t in self.tables) or any(
            c.custom for t in self.tables for c in t.columns
        )

    @property
    def undocumented_fraction(self) -> float:
        if not self.tables:
            return 0.0
        undocumented = sum(1 for t in self.tables if not t.description)
        return undocumented / len(self.tables)


def _is_bookkeeping(fk: ForeignKey) -> bool:
    return all(c in BOOKKEEPING_COLUMNS for c in fk.from_columns)


def _reference_counts(snapshot: SchemaSnapshot) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for table in snapshot.tables:
        for fk in table.foreign_keys:
            if not _is_bookkeeping(fk):
                counts[fk.to_table] += 1
    return counts


def find_hubs(snapshot: SchemaSnapshot, threshold: int = DEFAULT_HUB_THRESHOLD) -> set[str]:
    """Tables so widely referenced that their edges carry no local meaning."""
    return {name for name, count in _reference_counts(snapshot).items() if count >= threshold}


def _adjacency(snapshot: SchemaSnapshot, hubs: set[str]) -> dict[str, set[str]]:
    """Undirected FK graph with hub edges removed."""
    known = {t.name for t in snapshot.tables}
    graph: dict[str, set[str]] = {t.name: set() for t in snapshot.tables}
    for table in snapshot.tables:
        if table.name in hubs:
            continue
        for fk in table.foreign_keys:
            if _is_bookkeeping(fk):
                continue
            if fk.to_table in hubs or fk.to_table not in known or fk.to_table == table.name:
                continue
            graph[table.name].add(fk.to_table)
            graph[fk.to_table].add(table.name)
    return graph


def _seed_order(snapshot: SchemaSnapshot, graph: dict[str, set[str]]) -> list[str]:
    """Custom tables first -- they are the hard ones, and they should seed their own
    neighbourhood rather than arrive as an afterthought at the edge of someone else's."""
    return sorted(
        (t.name for t in snapshot.tables),
        key=lambda n: (
            0 if _tier_of(snapshot, n) is Tier.CUSTOM else 1,
            -len(graph.get(n, ())),
            n,
        ),
    )


def _tier_of(snapshot: SchemaSnapshot, name: str) -> Tier:
    table = snapshot.table(name)
    return table.tier if table else Tier.UNKNOWN


def _merge_singletons(clusters: list[Cluster], max_tables: int, max_columns: int) -> list[Cluster]:
    """Batch up the tables that have no business foreign keys at all.

    They have no neighbourhood, so grouping them is arbitrary either way -- and one API call per
    orphan table is the most expensive way to learn nothing.
    """
    orphans = [c for c in clusters if len(c.tables) == 1]
    if len(orphans) < 2:
        return clusters

    merged = [c for c in clusters if len(c.tables) > 1]
    batch: list[Table] = []
    for cluster in orphans:
        table = cluster.tables[0]
        over_tables = len(batch) + 1 > max_tables
        over_columns = sum(len(t.columns) for t in batch) + len(table.columns) > max_columns
        if batch and (over_tables or over_columns):
            merged.append(Cluster(tables=batch))
            batch = []
        batch.append(table)
    if batch:
        merged.append(Cluster(tables=batch))

    for index, cluster in enumerate(merged):
        cluster.index = index
    return merged


def cluster_schema(
    snapshot: SchemaSnapshot,
    max_tables: int = 12,
    max_columns: int = 150,
    hub_threshold: int = DEFAULT_HUB_THRESHOLD,
    max_context: int = 8,
) -> list[Cluster]:
    """Group tables into foreign-key neighbourhoods.

    Deterministic: the same snapshot always produces the same clusters, so a re-run is comparable
    to the run before it.
    """
    hubs = find_hubs(snapshot, hub_threshold)
    graph = _adjacency(snapshot, hubs)
    by_name = {t.name: t for t in snapshot.tables}

    unassigned = set(by_name)
    clusters: list[Cluster] = []

    for seed in _seed_order(snapshot, graph):
        if seed not in unassigned:
            continue
        chosen = [seed]
        unassigned.discard(seed)
        columns = len(by_name[seed].columns)

        # Breadth-first, so a cluster is a neighbourhood rather than a random walk.
        frontier = sorted(graph.get(seed, ()))
        while frontier and len(chosen) < max_tables:
            candidate = frontier.pop(0)
            if candidate not in unassigned:
                continue
            candidate_columns = len(by_name[candidate].columns)
            if columns + candidate_columns > max_columns and chosen:
                continue
            chosen.append(candidate)
            unassigned.discard(candidate)
            columns += candidate_columns
            frontier.extend(sorted(n for n in graph.get(candidate, ()) if n in unassigned))

        clusters.append(
            Cluster(index=len(clusters), tables=[by_name[n] for n in chosen])
        )

    clusters = _merge_singletons(clusters, max_tables, max_columns)

    # Context: everything a cluster points at but does not own -- hubs included. Named so the
    # model can reuse a concept instead of inventing a second one for the same table.
    owner = {name: c.index for c in clusters for name in c.table_names}
    for cluster in clusters:
        referenced: list[str] = []
        for table in cluster.tables:
            for fk in table.foreign_keys:
                target = fk.to_table
                if target in by_name and owner.get(target) != cluster.index:
                    if target not in referenced:
                        referenced.append(target)
        cluster.context = [by_name[n] for n in referenced[:max_context]]

    return clusters
