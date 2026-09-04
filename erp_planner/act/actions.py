"""Phase 6 -- acting on the ERP, in dry-run only.

The single most important line in the spec: **writes go through the ERP's own API, never direct
SQL.** Direct table writes bypass validations, business rules, the workflow engine, document
numbering and the permission model. A mapping that is 98% right still means a write to the wrong
table 2% of the time, and an ERP does not survive that quietly.

So this module plans actions and renders them. It does not execute. `plan()` produces the exact
Odoo ORM call that would run -- model, method, arguments, the record it would touch -- and
`Plan.render()` prints it. There is no execute path to disable, because none was written.

Two gates stand before execution is even worth building:

1. **Phase 3's catch rate is 50%** against a 90% target. Half of wrong mappings ship unflagged.
2. **Writes must be allow-listed against reviewed mappings**, and Phase 4 -- the review -- has not
   run, so no mapping in this system has been reviewed by anyone.

Until both change, a plan is the product.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from erp_planner.models import Ontology
from erp_planner.verify.verifier import VerificationReport


class Risk(StrEnum):
    BLOCKED = "blocked"  # the mapping it depends on is not trustworthy
    REVIEW = "review"  # allowed, but a human should look first
    ROUTINE = "routine"


class ActionKind(StrEnum):
    CREATE = "create"
    UPDATE = "write"
    RESERVE = "reserve"


class Action(BaseModel):
    """What the agent wants to do, in business terms."""

    kind: ActionKind
    concept: str = Field(description="The business concept, e.g. SalesOrder.")
    values: dict[str, str] = Field(default_factory=dict, description="Business field -> value.")
    record_id: int | None = None


class PlannedCall(BaseModel):
    """The sanctioned ERP call this would become. Rendered, never sent."""

    model: str
    method: str
    args: list[str] = Field(default_factory=list)
    kwargs: dict[str, str] = Field(default_factory=dict)

    def as_call(self) -> str:
        parts = [*self.args, *(f"{k}={v!r}" for k, v in self.kwargs.items())]
        return f"{self.model}.{self.method}({', '.join(parts)})"


class Plan(BaseModel):
    action: Action
    call: PlannedCall | None = None
    risk: Risk = Risk.BLOCKED
    reasons: list[str] = Field(default_factory=list)
    executed: bool = False  # always False; kept so a log cannot be mistaken for a run

    def render(self) -> str:
        lines = [
            "DRY RUN — nothing was sent to the ERP",
            f"  intent : {self.action.kind.value} {self.action.concept}",
            f"  risk   : {self.risk.value}",
        ]
        lines.append(f"  call   : {self.call.as_call()}" if self.call else "  call   : (not planned)")
        lines.extend(f"  note   : {r}" for r in self.reasons)
        return "\n".join(lines)


# Odoo model name from a table name: res_partner -> res.partner. The write path uses the ORM, so
# it needs the model, never the table.
def odoo_model_for(table: str) -> str:
    return table.replace("_", ".", table.count("_")) if "_" in table else table


def plan(
    action: Action,
    ontology: Ontology,
    verification: VerificationReport | None = None,
    allow_list: set[str] | None = None,
) -> Plan:
    """Turn an intent into the ERP call it would become, and decide whether it may ever run.

    Refusal is the default. A concept the ontology does not know, or a mapping verification
    flagged, cannot be written to at all -- "not flagged" is not the same as "reviewed", and the
    measured silent-error rate is why.
    """
    result = Plan(action=action)
    match = next((c for c in ontology.classes if c.label.lower() == action.concept.lower()), None)
    if match is None:
        result.reasons.append(f"no concept named {action.concept!r} in the ontology")
        return result

    table = match.table
    columns = {p.label.lower(): p.column for p in ontology.properties if p.table == table}
    unknown = [k for k in action.values if k.lower() not in columns]
    if unknown:
        result.reasons.append(f"unmapped fields: {', '.join(unknown)}")
        return result

    if verification is not None:
        flagged = {v.key for v in verification.flagged}
        if table in flagged:
            result.reasons.append(f"verification flagged the mapping for {table}")
            return result
        for field in action.values:
            key = f"{table}.{columns[field.lower()]}"
            if key in flagged:
                result.reasons.append(f"verification flagged {key}")
                return result

    if allow_list is not None and match.label not in allow_list:
        result.reasons.append(f"{match.label} is not on the write allow-list")
        return result

    result.call = PlannedCall(
        model=odoo_model_for(table),
        method=action.kind.value,
        args=[f"[{action.record_id}]"] if action.record_id else [],
        kwargs={columns[k.lower()]: v for k, v in action.values.items()},
    )
    # Even a fully permitted action is REVIEW, not ROUTINE: no mapping in this system has been
    # through Phase 4, so nothing has been reviewed by a human.
    result.risk = Risk.REVIEW
    result.reasons.append(
        "would be permitted, but no mapping here has been human-reviewed (Phase 4 has not run)"
    )
    return result
