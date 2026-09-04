"""Core interchange types.

Every stage of the pipeline speaks these types:

    ingest  -> SchemaSnapshot
    mapping -> Ontology            (the machine's proposal)
    Phase 0 -> Ontology            (the expert's gold standard, same shape on purpose)
    verify  -> Ontology + confidence/flags on each mapping

Keeping the machine proposal and the gold standard in *one* shape is deliberate: the scorer
compares like with like, and a human correction in Phase 4 is just a replacement mapping object.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------------------
# Schema side (output of ingestion)
# --------------------------------------------------------------------------------------


class Tier(StrEnum):
    """Difficulty tier of a table, per spec §1.

    The whole accuracy claim is scored per tier, never blended.
    """

    STANDARD = "standard"  # Tier 1 - documented vendor table, likely in LLM training data
    CUSTOM = "custom"  # Tier 2 - customer-specific, documented nowhere
    UNKNOWN = "unknown"


class Column(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    is_primary_key: bool = False
    # Whatever the ERP itself knows about this field (Odoo ir.model.fields.field_description,
    # SAP DDIC short text). Tier 2 tables frequently have this and nothing else.
    description: str | None = None
    # A customer-added field on an otherwise standard table (Odoo Studio, SAP append structure).
    # Tier is per table; this is Tier 2 living inside a Tier 1 table.
    custom: bool = False
    # Value *shape*, not raw values - see spec Phase 1 masking criterion.
    sample_values: list[str] = Field(default_factory=list)
    distinct_count: int | None = None
    null_fraction: float | None = None


class ForeignKey(BaseModel):
    from_columns: list[str]
    to_table: str
    to_columns: list[str]
    # Declared FK constraint vs. inferred (ERPNext link fields, Odoo many2one without a
    # database-level constraint). Phase 3's FK-neighborhood check weights these differently.
    declared: bool = True


class Table(BaseModel):
    name: str
    tier: Tier = Tier.UNKNOWN
    description: str | None = None
    row_count: int | None = None
    columns: list[Column] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


class SchemaSnapshot(BaseModel):
    """A machine-readable capture of one ERP schema at one point in time."""

    source: str  # "odoo", "erpnext", "sap", ...
    source_version: str | None = None
    dialect: str | None = None  # "postgresql", "mysql", ...
    captured_at: str | None = None
    # Free-text docs / data dictionary entries pulled during ingestion, keyed by table name.
    docs: dict[str, str] = Field(default_factory=dict)
    tables: list[Table] = Field(default_factory=list)
    # Set by the obfuscator; None means "real names".
    obfuscation: str | None = None
    # Set at ingestion; records which masking mode produced these sample values.
    masking: str | None = None

    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]


# --------------------------------------------------------------------------------------
# Ontology side (output of mapping; also the shape of the gold standard)
# --------------------------------------------------------------------------------------


class MappingKind(StrEnum):
    CLASS = "class"
    PROPERTY = "property"
    RELATION = "relation"


class ClassMapping(BaseModel):
    """A table means a concept.  res_partner -> Customer."""

    table: str
    label: str  # human-meaningful concept name
    parent: str | None = None  # is-a target, e.g. Customer -> Party
    confidence: float | None = None  # set by Phase 3, calibrated
    flagged: bool = False
    rationale: str | None = None

    @property
    def key(self) -> str:
        return self.table


class PropertyMapping(BaseModel):
    """A column means an attribute.  res_partner.vat -> taxIdentifier."""

    table: str
    column: str
    label: str
    datatype: str | None = None  # xsd type
    confidence: float | None = None
    flagged: bool = False
    rationale: str | None = None

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"


class RelationMapping(BaseModel):
    """A foreign key means a relationship.  sale_order.partner_id -> placedBy(Order, Customer)."""

    from_table: str
    from_columns: list[str]
    to_table: str
    label: str
    confidence: float | None = None
    flagged: bool = False
    rationale: str | None = None

    @property
    def key(self) -> str:
        return f"{self.from_table}[{','.join(self.from_columns)}]->{self.to_table}"


class Ontology(BaseModel):
    """The unit of comparison. A proposal and a gold standard are the same type."""

    name: str = "ontology"
    schema_source: str | None = None
    obfuscation: str | None = None
    classes: list[ClassMapping] = Field(default_factory=list)
    properties: list[PropertyMapping] = Field(default_factory=list)
    relations: list[RelationMapping] = Field(default_factory=list)
    # Provenance for cost/latency reporting (spec Phase 2 unit-economics criterion).
    run_metadata: dict[str, str | float | int] = Field(default_factory=dict)

    def mappings(self, kind: MappingKind) -> list[ClassMapping | PropertyMapping | RelationMapping]:
        return {
            MappingKind.CLASS: self.classes,
            MappingKind.PROPERTY: self.properties,
            MappingKind.RELATION: self.relations,
        }[kind]

    @property
    def total_mappings(self) -> int:
        return len(self.classes) + len(self.properties) + len(self.relations)
