"""Label normalisation and synonym equivalence.

Spec Phase 0: scoring must be "robust to naming variation (Customer vs Client vs
BusinessPartner scored via synonym/equivalence sets, not string match)".

String equality would punish a correct mapping for choosing a different word than the gold
author did, which would make every accuracy number an artefact of vocabulary taste.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

# Ships inside the package. It used to live in the repo's data/ directory, which is not
# packaged -- so an installed wheel loaded zero concepts and reported Customer and Client as
# different, silently.
DEFAULT_VOCABULARY = Path(__file__).resolve().parent / "data" / "business_concepts.yaml"

# Words that end in "s" but are not plurals.
_NOT_PLURAL = {
    "address",
    "analysis",
    "business",
    "class",
    "gross",
    "status",
    "process",
    "series",
    "vat",
    "goods",
    "terms",
}

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _singularise(word: str) -> str:
    if word in _NOT_PLURAL or len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("ses") or word.endswith("xes") or word.endswith("ches"):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def normalise(label: str) -> str:
    """Reduce a label to a comparable canonical string.

    ``BusinessPartner`` / ``business_partner`` / ``Business Partners`` -> ``business partner``
    """
    if not label:
        return ""
    spaced = _CAMEL.sub(" ", label)
    spaced = re.sub(r"[^0-9a-zA-Z]+", " ", spaced)
    words = [_singularise(w.lower()) for w in spaced.split() if w]
    return " ".join(words)


class Vocabulary:
    """Equivalence sets over business concepts.

    The YAML is ``canonical: [synonym, ...]``.  A label resolves to its canonical concept if it
    normalises to the canonical name or to any listed synonym; otherwise it resolves to its own
    normalised form (so unknown-but-identical labels still match each other).
    """

    def __init__(self, groups: dict[str, list[str]] | None = None) -> None:
        self._synonym_to_canonical: dict[str, str] = {}
        # Space-free index, so mixed-case acronyms reach their synonym: "UoM" normalises to
        # "uo m" (the camel splitter cannot know "UoM" is one token), which collapses to "uom".
        self._collapsed_to_canonical: dict[str, str] = {}
        self.groups = groups or {}
        for canonical, synonyms in self.groups.items():
            canon_norm = normalise(canonical)
            self._register(canon_norm, canon_norm)
            for syn in synonyms or []:
                self._register(normalise(syn), canon_norm)

    def _register(self, norm: str, canonical: str) -> None:
        self._synonym_to_canonical[norm] = canonical
        self._collapsed_to_canonical.setdefault(norm.replace(" ", ""), canonical)

    @classmethod
    def load(cls, path: Path | str | None = None) -> Vocabulary:
        """Load the equivalence sets.

        A missing *default* file is a packaging fault and raises: an empty vocabulary silently
        turns every concept into an unknown one, stops reconciliation merging anything, and makes
        the scorer treat Customer and Client as different. A missing file the caller *named* is
        their business, and returns empty.
        """
        if path is None:
            if not DEFAULT_VOCABULARY.exists():
                raise FileNotFoundError(
                    f"the concept vocabulary is missing from the installed package "
                    f"({DEFAULT_VOCABULARY}). Every concept would be treated as unknown."
                )
            path = DEFAULT_VOCABULARY
        path = Path(path)
        if not path.exists():
            return cls({})
        data = yaml.safe_load(path.read_text()) or {}
        return cls(data.get("concepts", data))

    def canonical(self, label: str) -> str:
        norm = normalise(label)
        if norm in self._synonym_to_canonical:
            return self._synonym_to_canonical[norm]
        return self._collapsed_to_canonical.get(norm.replace(" ", ""), norm)

    def equivalent(self, a: str, b: str) -> bool:
        return bool(a) and bool(b) and self.canonical(a) == self.canonical(b)


@lru_cache(maxsize=1)
def default_vocabulary() -> Vocabulary:
    return Vocabulary.load()
