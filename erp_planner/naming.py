"""Naming is code, not model judgement.

The same principle that made routing reliable, applied to labels. The model supplies *meaning* --
the bare attribute a column records, the role a foreign key plays -- and this module renders it
into a name. Three things follow:

* **Names are identical across ERPs by construction.** SAP's ``KNA1`` and Odoo's ``res_partner``
  produce the same property names for the same meanings, which is what makes one shared semantic
  layer over two ERPs possible at all.
* **The convention is configuration.** Changing it is an edit and a re-run, not a re-prompt, and
  two runs stay comparable.
* **The scorer can compare meanings.** The first scored run produced fifteen "errors" of which
  none were wrong meanings -- ``name`` vs ``productName``, ``customer`` vs ``placedBy``. The model
  was right every time and was marked wrong for spelling, because nothing had ever told it which
  convention to use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORDS = re.compile(r"[A-Za-z0-9]+")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Attributes too generic to stand alone: every class has a name, a code, a date. These get
# qualified with their class (`name` on Product -> `productName`); anything more specific does
# not (`list price` stays `listPrice`, not `productListPrice`).
GENERIC_ATTRIBUTES = frozenset(
    {
        "name",
        "code",
        "type",
        "value",
        "date",
        "time",
        "amount",
        "status",
        "state",
        "description",
        "reference",
        "number",
        "label",
        "title",
        "id",
        "identifier",
        "category",
        "kind",
        "quantity",
        "note",
    }
)


@dataclass(frozen=True)
class NamingConvention:
    """How meanings become names. Swap it and re-run; do not re-prompt."""

    qualify_generic: bool = True
    generic_attributes: frozenset[str] = field(default=GENERIC_ATTRIBUTES)

    def class_name(self, concept: str) -> str:
        """A business concept, PascalCase and singular-ish. ``sales order`` -> ``SalesOrder``."""
        return "".join(w[:1].upper() + w[1:] for w in words_of(concept))

    def property_name(self, class_label: str, attribute: str) -> str:
        """``Product`` + ``name`` -> ``productName``;  ``Product`` + ``list price`` -> ``listPrice``."""
        attribute_words = words_of(attribute)
        if not attribute_words:
            return ""
        if self.qualify_generic and " ".join(attribute_words) in self.generic_attributes:
            class_words = words_of(class_label)
            # Don't stutter: `Country` + `country name` stays `countryName`.
            if class_words and class_words[-1] != attribute_words[0]:
                attribute_words = class_words + attribute_words
        return camel(attribute_words)

    def relation_name(self, role: str) -> str:
        """``placed by`` -> ``placedBy``; ``located in country`` -> ``locatedInCountry``."""
        return camel(words_of(role))


DEFAULT_CONVENTION = NamingConvention()


def words_of(text: str) -> list[str]:
    """Split any casing or separator style into lowercase words."""
    if not text:
        return []
    spaced = _CAMEL_SPLIT.sub(" ", text)
    return [w.lower() for w in _WORDS.findall(spaced)]


def camel(words: list[str]) -> str:
    if not words:
        return ""
    return words[0] + "".join(w[:1].upper() + w[1:] for w in words[1:])


def strip_class_qualifier(label: str, class_label: str) -> str:
    """Remove a leading class name from a property label, for comparison.

    ``productName`` on class ``Product`` -> ``name``, so it compares equal to a bare ``name``.
    Applied to both sides of a comparison, so it never favours one author's habit over another's.
    It removes only a *qualifier*: ``legalName`` on ``Party`` stays ``legal name``, because that
    is a different meaning rather than a different spelling.
    """
    label_words = words_of(label)
    class_words = set(words_of(class_label))
    if not label_words or not class_words:
        return " ".join(label_words)
    # Drop leading words that appear anywhere in the class name, not just at its end: `unitName`
    # is qualified by the *first* word of `UnitOfMeasure`, and a suffix-only rule misses it.
    index = 0
    while index < len(label_words) - 1 and label_words[index] in class_words:
        index += 1
    return " ".join(label_words[index:])
