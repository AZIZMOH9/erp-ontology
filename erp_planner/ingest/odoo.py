"""Odoo ingestion connector.

Two sources, because neither alone is enough:

* **Postgres** gives structure -- tables, columns, types, primary and foreign keys, and the
  sample values meaning is actually inferred from.
* **Odoo's XML-RPC API** gives ``ir.model`` / ``ir.model.fields``: human field labels, help text,
  relation targets, and -- critically -- ``state='manual'``, which is how Odoo itself records that
  a model or field was added by a customer rather than shipped by Odoo. That is the Tier 2 signal,
  straight from the horse's mouth, instead of guessing from an ``x_`` prefix.

Scale is handled by asking Postgres what it already knows rather than scanning:
``pg_class.reltuples`` for row counts and ``pg_stats`` for null fractions and distinct counts are
free, because ANALYZE already paid for them. Only sample rows cost a query, one per table.
"""

from __future__ import annotations

import xmlrpc.client
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine, inspect, text

from erp_planner.models import Column, ForeignKey, SchemaSnapshot, Table, Tier

# Odoo's own plumbing. Mapping it wastes tokens and pollutes the ontology with concepts no
# business user has ever heard of. Excluded by default, overridable.
DEFAULT_EXCLUDE_PREFIXES = (
    "ir_",
    "res_groups",
    "res_users_",
    "base_",
    "bus_",
    "mail_tracking_",
    "report_",
    "wizard_",
    "web_",
    "iap_",
)


@dataclass
class OdooModelMeta:
    """What Odoo says about one of its models."""

    model: str
    table: str
    description: str | None = None
    manual: bool = False
    fields: dict[str, OdooFieldMeta] = field(default_factory=dict)


@dataclass
class OdooFieldMeta:
    name: str
    label: str | None = None
    help: str | None = None
    ttype: str | None = None
    relation: str | None = None
    manual: bool = False

    def as_description(self) -> str | None:
        """Collapse Odoo's label and help text into one description line."""
        parts = [p for p in (self.label, self.help) if p]
        if self.ttype and self.relation:
            parts.append(f"({self.ttype} -> {self.relation})")
        return " — ".join(parts) if parts else None


# --------------------------------------------------------------------------------------
# Odoo metadata over XML-RPC
# --------------------------------------------------------------------------------------


def fetch_odoo_metadata(
    url: str, db: str, username: str, password: str, timeout: int = 60
) -> dict[str, OdooModelMeta]:
    """Read ``ir.model`` and ``ir.model.fields``. Returns metadata keyed by *table* name."""
    transport = xmlrpc.client.Transport()
    transport.timeout = timeout  # type: ignore[attr-defined]
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", transport=transport)
    uid = common.authenticate(db, username, password, {})
    if not uid:
        raise RuntimeError(f"Odoo authentication failed for {username!r} on {db!r}")

    proxy = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", transport=transport)

    def call(model: str, method: str, *args, **kwargs):
        return proxy.execute_kw(db, uid, password, model, method, list(args), kwargs)

    models: dict[str, OdooModelMeta] = {}
    for row in call(
        "ir.model", "search_read", [], fields=["model", "name", "state"]
    ):
        table = row["model"].replace(".", "_")
        models[table] = OdooModelMeta(
            model=row["model"],
            table=table,
            description=row.get("name") or None,
            manual=row.get("state") == "manual",
        )

    for row in call(
        "ir.model.fields",
        "search_read",
        [],
        fields=["model", "name", "field_description", "help", "ttype", "relation", "state"],
    ):
        table = row["model"].replace(".", "_")
        meta = models.get(table)
        if meta is None:
            continue
        meta.fields[row["name"]] = OdooFieldMeta(
            name=row["name"],
            label=row.get("field_description") or None,
            help=row.get("help") or None,
            ttype=row.get("ttype") or None,
            relation=row.get("relation") or None,
            manual=row.get("state") == "manual",
        )
    return models


# --------------------------------------------------------------------------------------
# Postgres introspection
# --------------------------------------------------------------------------------------


def _row_counts(engine: Engine, schema: str) -> dict[str, int]:
    """Estimated row counts from the planner's own statistics -- no table scans."""
    sql = text(
        """
        SELECT c.relname, GREATEST(c.reltuples, 0)::bigint AS rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema AND c.relkind = 'r'
        """
    )
    with engine.connect() as conn:
        return {r.relname: int(r.rows) for r in conn.execute(sql, {"schema": schema})}


def _column_stats(engine: Engine, schema: str) -> dict[tuple[str, str], tuple[float | None, int | None]]:
    """Null fraction and distinct count from ``pg_stats``.

    ``n_distinct`` is negative when Postgres expresses it as a fraction of the table, so it is
    converted back to an absolute count using the row estimate.
    """
    sql = text(
        """
        SELECT s.tablename, s.attname, s.null_frac, s.n_distinct,
               GREATEST(c.reltuples, 0)::bigint AS rows
        FROM pg_stats s
        JOIN pg_class c ON c.relname = s.tablename
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = s.schemaname
        WHERE s.schemaname = :schema
        """
    )
    out: dict[tuple[str, str], tuple[float | None, int | None]] = {}
    with engine.connect() as conn:
        for row in conn.execute(sql, {"schema": schema}):
            n_distinct = row.n_distinct
            if n_distinct is None:
                distinct = None
            elif n_distinct < 0:
                distinct = int(round(-n_distinct * row.rows)) or None
            else:
                distinct = int(n_distinct) or None
            out[(row.tablename, row.attname)] = (row.null_frac, distinct)
    return out


def _sample_values(
    engine: Engine, schema: str, table: str, columns: list[str], rows: int, per_column: int
) -> dict[str, list[str]]:
    """One query per table. Distinct non-null values per column, taken from that sample."""
    if not columns:
        return {}
    quoted = ", ".join(f'"{c}"' for c in columns)
    sql = text(f'SELECT {quoted} FROM "{schema}"."{table}" LIMIT :limit')
    seen: dict[str, list[str]] = {c: [] for c in columns}
    with engine.connect() as conn:
        for row in conn.execute(sql, {"limit": rows}):
            for column, value in zip(columns, row, strict=True):
                bucket = seen[column]
                if value is None or len(bucket) >= per_column:
                    continue
                rendered = str(value)
                if rendered and rendered not in bucket:
                    bucket.append(rendered)
    return {c: v for c, v in seen.items() if v}


def _tier_for(
    table: str,
    meta: OdooModelMeta | None,
    columns: list[Column],
    foreign_keys: list[ForeignKey],
) -> Tier:
    """Odoo records customisations itself; trust that before falling back to convention.

    Roughly a third of an Odoo database has no ``ir.model`` entry at all -- these are the
    many-to-many join tables Odoo generates, which are nothing but two foreign keys. They are
    vendor-shipped structure, so calling them Tier 1 is accurate; calling them "unknown" would
    pad the hard tier with the easiest tables in the database and flatter every Tier 2 number.
    """
    if meta is not None:
        return Tier.CUSTOM if meta.manual else Tier.STANDARD
    if table.startswith("x_"):
        return Tier.CUSTOM
    fk_columns = {c for fk in foreign_keys for c in fk.from_columns}
    if len(foreign_keys) >= 2 and all(c.name in fk_columns or c.is_primary_key for c in columns):
        return Tier.STANDARD
    return Tier.UNKNOWN


def ingest_odoo(
    db_url: str,
    *,
    schema: str = "public",
    odoo_url: str | None = None,
    odoo_db: str | None = None,
    odoo_user: str | None = None,
    odoo_password: str | None = None,
    sample_rows: int = 200,
    values_per_column: int = 5,
    include_prefixes: tuple[str, ...] = (),
    exclude_prefixes: tuple[str, ...] = DEFAULT_EXCLUDE_PREFIXES,
    max_tables: int | None = None,
) -> SchemaSnapshot:
    """Produce a :class:`SchemaSnapshot` for one Odoo database.

    ``odoo_url`` is optional: without it the connector still works from Postgres alone, losing
    field labels and reliable custom-model detection. With it, every table carries Odoo's own
    description and every column its human label.
    """
    engine = create_engine(db_url)
    inspector = inspect(engine)

    metadata: dict[str, OdooModelMeta] = {}
    if odoo_url:
        metadata = fetch_odoo_metadata(
            odoo_url, odoo_db or "", odoo_user or "", odoo_password or ""
        )

    names = sorted(inspector.get_table_names(schema=schema))
    if include_prefixes:
        names = [n for n in names if n.startswith(include_prefixes)]
    # A customisation always wins over an exclusion: an x_ table is the whole point of Tier 2.
    names = [
        n
        for n in names
        if not n.startswith(exclude_prefixes)
        or n.startswith("x_")
        or (n in metadata and metadata[n].manual)
    ]
    if max_tables:
        names = names[:max_tables]

    counts = _row_counts(engine, schema)
    stats = _column_stats(engine, schema)

    tables: list[Table] = []
    docs: dict[str, str] = {}
    for name in names:
        meta = metadata.get(name)
        pk = set(inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or [])
        raw_fks = inspector.get_foreign_keys(name, schema=schema)
        fk_columns = {c for fk in raw_fks for c in fk["constrained_columns"]}
        raw_columns = inspector.get_columns(name, schema=schema)
        # Surrogate keys carry no meaning -- sampling them only costs tokens in every mapping
        # prompt. The relationship they encode is captured by the foreign key itself.
        sampled = [c["name"] for c in raw_columns if c["name"] not in pk and c["name"] not in fk_columns]
        samples = _sample_values(engine, schema, name, sampled, sample_rows, values_per_column)

        columns: list[Column] = []
        for raw in raw_columns:
            null_frac, distinct = stats.get((name, raw["name"]), (None, None))
            field_meta = meta.fields.get(raw["name"]) if meta else None
            columns.append(
                Column(
                    name=raw["name"],
                    data_type=str(raw["type"]),
                    nullable=bool(raw.get("nullable", True)),
                    is_primary_key=raw["name"] in pk,
                    description=field_meta.as_description() if field_meta else None,
                    custom=bool(field_meta and field_meta.manual),
                    sample_values=samples.get(raw["name"], []),
                    distinct_count=distinct,
                    null_fraction=null_frac,
                )
            )

        foreign_keys = [
            ForeignKey(
                from_columns=list(fk["constrained_columns"]),
                to_table=fk["referred_table"],
                to_columns=list(fk["referred_columns"]),
                declared=True,
            )
            for fk in raw_fks
            if fk.get("referred_table")
        ]

        tables.append(
            Table(
                name=name,
                tier=_tier_for(name, meta, columns, foreign_keys),
                description=meta.description if meta else None,
                row_count=counts.get(name),
                columns=columns,
                foreign_keys=foreign_keys,
            )
        )
        if meta and meta.description:
            docs[name] = f"Odoo model {meta.model}: {meta.description}"

    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar()

    return SchemaSnapshot(
        source="odoo",
        source_version=odoo_db or schema,
        dialect=f"postgresql {version}",
        captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        docs=docs,
        tables=tables,
    )
