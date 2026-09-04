"""What the model is asked to return.

These are the structured-output schema, kept separate from :mod:`erp_planner.models` on purpose:
this shape is tuned for a language model to fill in (flat, one relation per foreign key column,
every field required), while the pipeline's own types are tuned for scoring and review. The
translation between them is one function, :func:`to_ontology`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from erp_planner.models import ClassMapping, Ontology, PropertyMapping, RelationMapping
from erp_planner.naming import DEFAULT_CONVENTION, NamingConvention


class ProposedClass(BaseModel):
    table: str = Field(description="Exact table name from the schema.")
    label: str = Field(description="The business concept this table records, e.g. Customer.")
    parent: str | None = Field(
        default=None, description="Broader concept this is a kind of, or null."
    )
    confidence: float = Field(description="0.0 to 1.0. Be honest; low is useful information.")
    rationale: str = Field(description="One sentence: the evidence you used.")


class ProposedProperty(BaseModel):
    table: str
    column: str = Field(description="Exact column name from the schema.")
    attribute: str = Field(
        description=(
            "What this column records, in plain lowercase words: 'name', 'tax identifier', "
            "'list price'. Do NOT format it or add the class name - that is applied afterwards."
        )
    )
    datatype: str = Field(description="An xsd type, e.g. xsd:string.")
    confidence: float


class ProposedRelation(BaseModel):
    from_table: str
    from_column: str = Field(description="Exact foreign key column name.")
    to_table: str
    role: str = Field(
        description=(
            "What the source DOES to the target, as a short verb phrase in plain lowercase "
            "words, read source-to-target: 'placed by', 'located in country', 'measured in'. "
            "Never just name the target ('customer'); say the relationship."
        )
    )
    confidence: float


class ClusterProposal(BaseModel):
    """One request's answer."""

    classes: list[ProposedClass]
    properties: list[ProposedProperty]
    relations: list[ProposedRelation]


def to_ontology(
    proposals: list[ClusterProposal],
    schema_source: str | None = None,
    convention: NamingConvention | None = None,
) -> Ontology:
    """Fold proposals into the pipeline's ontology type, applying the naming convention.

    The model gave meanings; the names are rendered here, so they are identical for the same
    meaning no matter which ERP, model or run produced them.
    """
    naming = convention or DEFAULT_CONVENTION
    ontology = Ontology(name="mapped", schema_source=schema_source)
    # A property is named relative to its class, so classes are resolved first.
    class_of: dict[str, str] = {}
    for proposal in proposals:
        for c in proposal.classes:
            class_of.setdefault(c.table, naming.class_name(c.label))

    for proposal in proposals:
        for c in proposal.classes:
            ontology.classes.append(
                ClassMapping(
                    table=c.table,
                    label=naming.class_name(c.label),
                    parent=c.parent,
                    confidence=c.confidence,
                    rationale=c.rationale,
                )
            )
        for p in proposal.properties:
            ontology.properties.append(
                PropertyMapping(
                    table=p.table,
                    column=p.column,
                    label=naming.property_name(class_of.get(p.table, p.table), p.attribute),
                    datatype=p.datatype,
                    confidence=p.confidence,
                )
            )
        for r in proposal.relations:
            ontology.relations.append(
                RelationMapping(
                    from_table=r.from_table,
                    from_columns=[r.from_column],
                    to_table=r.to_table,
                    label=naming.relation_name(r.role),
                    confidence=r.confidence,
                )
            )
    return ontology
