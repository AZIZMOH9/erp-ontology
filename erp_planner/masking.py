"""Data minimisation.

The product runs inside the customer's network, but the LLM call does not: a schema fragment and
its sample values are what actually leave. This module decides what those values look like.

Masking is shape-preserving on purpose. Inferring that a column is a tax identifier needs to know
it looks like ``AA999999999`` and has 4,213 distinct values; it does not need to know that one of
them is ``DE811907980``. Shape and distribution carry nearly all the signal, and none of the risk.

Three modes, chosen per run:

    all         every sample value is masked                      (default)
    sensitive   only columns classified as sensitive are masked
    none        raw values are sent

The same primitive backs the benchmark's obfuscated split -- masking for privacy and obfuscation
for contamination control are the same operation with different motives.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from erp_planner.models import SchemaSnapshot


class MaskingMode(StrEnum):
    ALL = "all"
    SENSITIVE = "sensitive"
    NONE = "none"


_DIGIT = re.compile(r"\d")
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")


def mask_value(value: str) -> str:
    """Replace a value with its shape.

    ``ACME GmbH`` -> ``AAAA AaaA``;  ``DE89 3704 0044`` -> ``AA99 9999 9999``.

    Note this collapses distinct short values (``A``/``B``/``C`` -> ``A``/``A``/``A``), which is
    why ``distinct_count`` is captured separately at ingestion: after masking it is the only
    surviving signal of a column's real cardinality.
    """
    masked = _DIGIT.sub("9", value)
    masked = _UPPER.sub("A", masked)
    return _LOWER.sub("a", masked)


# --------------------------------------------------------------------------------------
# Sensitivity classification
# --------------------------------------------------------------------------------------

# Column-name patterns. Deliberately broad: a false positive costs a little mapping accuracy,
# a false negative sends real personal data to a third party.
#
# Boundaries allow a trailing digit, because ERPs number their repeated fields: Odoo ships
# `street2`, and `address1` / `line2` / `phone2` are everywhere. Requiring a clean word boundary
# would let every one of them through.
_NAME_PATTERNS: list[tuple[str, str]] = [
    (r"(^|_)(vat|tin|tax_id|ssn|nin|siret|siren)\d*($|_)", "tax_or_national_id"),
    (r"(^|_)(email|mail)\d*($|_)", "email"),
    (r"(^|_)(phone|mobile|fax|tel)\d*($|_)", "phone"),
    (r"(^|_)(iban|bic|swift|bank_acc|account_number|card|cc_num)\d*($|_)", "financial_account"),
    (r"(^|_)(password|passwd|pwd|secret|token|api_key|signature)\d*($|_)", "credential"),
    (r"(^|_)(salary|wage|payslip|compensation|bonus)\d*($|_)", "payroll"),
    (r"(^|_)(street|zip|postcode|postal|city|address)\d*($|_)", "address"),
    (r"(^|_)(birthday|birthdate|dob|date_of_birth)\d*($|_)", "date_of_birth"),
    (r"(^|_)(name|firstname|lastname|surname|display_name|complete_name)\d*($|_)", "person_or_org_name"),
    (r"(^|_)(note|comment|description|message|body|remark)\d*($|_)", "free_text"),
    (r"(^|_)(identity|passport|id_card|licence|license_no)\d*($|_)", "identity_document"),
]

# Value-shape patterns, for columns whose name gives nothing away (x_f42, zz_val3).
_VALUE_PATTERNS: list[tuple[str, str]] = [
    (r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", "email"),
    (r"^[A-Z]{2}\d{2}[A-Z0-9 ]{10,30}$", "financial_account"),
    (r"^\+?\d[\d\s().-]{7,}$", "phone"),
    (r"^\d{13,19}$", "financial_account"),
]

_COMPILED_NAMES = [(re.compile(p), c) for p, c in _NAME_PATTERNS]
_COMPILED_VALUES = [(re.compile(p), c) for p, c in _VALUE_PATTERNS]

# Long free text is treated as sensitive regardless of shape: it is where humans put anything.
_FREE_TEXT_LENGTH = 120

# Value-shape rules only make sense on text columns. A DATE column's "2026-01-15" matches the
# phone pattern perfectly, and masking dates throws away range and recency -- real mapping signal,
# no privacy gained, since a bare date identifies nobody.
_TEXTUAL_TYPES = ("char", "text", "varchar", "string", "json", "bytea", "enum", "uuid")


def _is_textual(data_type: str) -> bool:
    lowered = data_type.lower()
    return not lowered or any(t in lowered for t in _TEXTUAL_TYPES)


class Classification(BaseModel):
    sensitive: bool
    category: str | None = None
    reason: str = ""


def classify_column(
    column_name: str,
    data_type: str = "",
    sample_values: list[str] | None = None,
) -> Classification:
    """Decide whether a column's values are sensitive.

    Name patterns first (cheap, and covers standard ERP fields), then value shapes (covers the
    custom columns whose names mean nothing). Only used in ``sensitive`` mode.
    """
    lowered = column_name.lower()
    for pattern, category in _COMPILED_NAMES:
        if pattern.search(lowered):
            return Classification(
                sensitive=True, category=category, reason=f"column name matches {category}"
            )

    if _is_textual(data_type):
        for value in sample_values or []:
            for pattern, category in _COMPILED_VALUES:
                if pattern.match(value.strip()):
                    return Classification(
                        sensitive=True,
                        category=category,
                        reason=f"sample value looks like {category}",
                    )
            if len(value) > _FREE_TEXT_LENGTH:
                return Classification(
                    sensitive=True, category="free_text", reason="long free-text values"
                )

    if data_type.lower() in {"text", "jsonb", "json", "bytea"}:
        return Classification(
            sensitive=True, category="free_text", reason=f"unbounded {data_type} column"
        )
    return Classification(sensitive=False)


# --------------------------------------------------------------------------------------
# Applying it
# --------------------------------------------------------------------------------------


class MaskingReport(BaseModel):
    """What actually happened, so it can be shown to the customer before anything is sent."""

    mode: MaskingMode
    columns_total: int = 0
    columns_with_samples: int = 0
    columns_masked: int = 0
    columns_raw: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
    # Columns left raw in `sensitive` mode, for spot-checking the classifier's misses.
    raw_columns: list[str] = Field(default_factory=list)


def apply_masking(snapshot: SchemaSnapshot, mode: MaskingMode) -> MaskingReport:
    """Mask ``snapshot`` in place and return a report of what was done.

    In-place because a snapshot holding raw values should not outlive the decision to mask them.
    """
    report = MaskingReport(mode=mode)

    for table in snapshot.tables:
        for column in table.columns:
            report.columns_total += 1
            if not column.sample_values:
                continue
            report.columns_with_samples += 1

            if mode is MaskingMode.NONE:
                should_mask, category = False, None
            elif mode is MaskingMode.ALL:
                should_mask, category = True, "all"
            else:
                verdict = classify_column(column.name, column.data_type, column.sample_values)
                should_mask, category = verdict.sensitive, verdict.category

            if should_mask:
                column.sample_values = [mask_value(v) for v in column.sample_values]
                report.columns_masked += 1
                if category:
                    report.by_category[category] = report.by_category.get(category, 0) + 1
            else:
                report.columns_raw += 1
                if mode is MaskingMode.SENSITIVE:
                    report.raw_columns.append(f"{table.name}.{column.name}")

    snapshot.masking = mode.value
    return report
