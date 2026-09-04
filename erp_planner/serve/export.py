"""Export the ontology into formats other tools can open.

The spec's Phase 2 output is "a generated ontology (OWL/RDF)" -- ours was project-specific JSON
that nothing else could read. Four formats, each for a different reader:

``ttl``      OWL/Turtle. The standard artefact, and what a customer with an existing ontology
             stack expects. Opens in Protege and WebVOWL, which draw a better interactive graph
             than anything worth building here.
``dot``      Graphviz. Renders straight to SVG or PNG -- the picture you can put in a slide.
``graphml``  Gephi, yEd, Cytoscape, for laying out something this size by hand.
``mermaid``  Pastes into markdown and renders on GitHub.

A 411-concept ontology is unreadable as one picture, so the graph formats take a focus concept and
a depth, and draw its neighbourhood instead of everything.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

from erp_planner.models import Ontology

BASE = "http://erp-planner.local/ontology#"


def _iri(label: str) -> str:
    """A label as an IRI-safe local name."""
    cleaned = re.sub(r"[^0-9A-Za-z_]", "", label) or "Unnamed"
    return cleaned if not cleaned[0].isdigit() else f"_{cleaned}"


# --------------------------------------------------------------------------------------
# OWL / Turtle
# --------------------------------------------------------------------------------------


def unique_iris(ontology: Ontology) -> dict[str, str]:
    """A distinct IRI per table, even when two tables were given the same label.

    Seventeen concept names in the 418-table Odoo ontology are used by more than one table --
    `OrderLine` by four, including both `sale_order_line` and `purchase_order_line`. Minting the
    IRI from the label alone merges them into one class and silently loses the distinction, which
    is worse than the duplicate name itself. Colliding names are suffixed with their table.
    """
    by_label: dict[str, list[str]] = {}
    for c in ontology.classes:
        by_label.setdefault(c.label, []).append(c.table)
    out: dict[str, str] = {}
    for c in ontology.classes:
        tables = by_label[c.label]
        out[c.table] = _iri(c.label) if len(tables) == 1 else f"{_iri(c.label)}_{_iri(c.table)}"
    return out


def to_turtle(ontology: Ontology, base: str = BASE) -> str:
    """OWL in Turtle: classes, datatype properties, object properties, with domains and ranges."""
    iri_of = unique_iris(ontology)
    class_of = {c.table: c.label for c in ontology.classes}
    # Counted on labels, before disambiguation. Counting the IRIs afterwards always gives zero,
    # since making them distinct is the whole point of unique_iris().
    collisions = len(ontology.classes) - len({c.label for c in ontology.classes})
    lines = [
        f"@prefix : <{base}> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
        f'<{base.rstrip("#")}> a owl:Ontology .',
        "",
        "# Classes — one per table",
    ]
    if collisions:
        lines.insert(
            -1,
            f"# NOTE: {collisions} concept names are used by more than one table; their IRIs are\n"
            "# suffixed with the table so distinct concepts are not merged into one class.",
        )
    # A parent is named by label, so it has to be resolved to an IRI that this file declares.
    # Writing :{label} directly produced axioms pointing at nothing: 14 of 16 in a real run.
    # Either the label belongs to several tables and was suffixed by unique_iris, so the bare
    # name does not exist -- or no table carries it at all, and it was never declared.
    tables_of_label: dict[str, list[str]] = {}
    for c in ontology.classes:
        tables_of_label.setdefault(c.label, []).append(c.table)
    abstract_parents = sorted(
        {c.parent for c in ontology.classes if c.parent and c.parent not in tables_of_label}
    )
    unresolved = sum(
        1 for c in ontology.classes if c.parent and len(tables_of_label.get(c.parent, [])) > 1
    )
    if unresolved:
        lines.insert(
            -1,
            f"# NOTE: {unresolved} subClassOf axioms are omitted: their parent name is used by\n"
            "# more than one table, so which class was meant cannot be determined.",
        )
    for c in sorted(ontology.classes, key=lambda c: c.label):
        lines.append(f":{iri_of[c.table]} a owl:Class ;")
        lines.append(f'    rdfs:label "{c.label}" ;')
        if c.parent:
            candidates = tables_of_label.get(c.parent, [])
            if len(candidates) == 1:
                lines.append(f"    rdfs:subClassOf :{iri_of[candidates[0]]} ;")
            elif not candidates:
                lines.append(f"    rdfs:subClassOf :{_iri(c.parent)} ;")
            # Several candidates: the axiom is dropped rather than pointed at a guess.
        # The physical table the class came from, so a mapping can be traced back.
        lines.append(f'    rdfs:comment "table: {c.table}" .')
        lines.append("")

    # Parents no table carries -- Person, Organisation, Address. They are real classes of this
    # ontology, so they are declared; leaving them undeclared made every axiom naming them
    # dangle, which is what a reasoner or Protege complains about.
    if abstract_parents:
        lines.append("# Classes with no table of their own — parents the mapping proposed")
        for label in abstract_parents:
            lines.append(f":{_iri(label)} a owl:Class ;")
            lines.append(f'    rdfs:label "{label}" ;')
            lines.append('    rdfs:comment "no table maps to this class; it is a parent only" .')
            lines.append("")

    lines.append("# Datatype properties — one per column")
    # A property name reused across classes is one property with several domains. Asserting all
    # of them would mean their intersection in OWL, which is wrong, so the domain is stated only
    # when the property belongs to exactly one class.
    domains: dict[str, set[str]] = {}
    for p in ontology.properties:
        domains.setdefault(_iri(p.label), set()).add(p.table)
    seen: set[str] = set()
    for p in sorted(ontology.properties, key=lambda p: p.label):
        name = _iri(p.label)
        if name in seen:
            continue
        seen.add(name)
        lines.append(f":{name} a owl:DatatypeProperty ;")
        lines.append(f'    rdfs:label "{p.label}" ;')
        if len(domains[name]) == 1 and p.table in class_of:
            lines.append(f"    rdfs:domain :{iri_of[p.table]} ;")
        lines.append(f"    rdfs:range {p.datatype or 'xsd:string'} ;")
        lines.append(f'    rdfs:comment "column: {p.key}" .')
        lines.append("")

    lines.append("# Object properties — one per foreign key")
    seen = set()
    for r in sorted(ontology.relations, key=lambda r: r.label):
        name = _iri(r.label)
        if name in seen:
            continue
        seen.add(name)
        lines.append(f":{name} a owl:ObjectProperty ;")
        lines.append(f'    rdfs:label "{r.label}" ;')
        if r.from_table in iri_of:
            lines.append(f"    rdfs:domain :{iri_of[r.from_table]} ;")
        if r.to_table in iri_of:
            lines.append(f"    rdfs:range :{iri_of[r.to_table]} ;")
        lines.append(f'    rdfs:comment "foreign key: {r.key}" .')
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Choosing what to draw
# --------------------------------------------------------------------------------------


def neighbourhood(
    ontology: Ontology, focus: str | None = None, depth: int = 1, limit: int = 40
) -> set[str]:
    """Which tables to draw. Everything is unreadable; a neighbourhood is not.

    Without a focus, the most connected concepts are chosen -- the backbone of the schema, which
    is what someone wants to see first.
    """
    edges: dict[str, set[str]] = {}
    for r in ontology.relations:
        edges.setdefault(r.from_table, set()).add(r.to_table)
        edges.setdefault(r.to_table, set()).add(r.from_table)

    if focus is None:
        ranked = sorted(edges, key=lambda t: (-len(edges[t]), t))
        return set(ranked[:limit])

    match = next(
        (c.table for c in ontology.classes if c.label.lower() == focus.lower() or c.table == focus),
        None,
    )
    if match is None:
        return set()
    seen, frontier = {match}, {match}
    for _ in range(depth):
        nxt: set[str] = set()
        for table in frontier:
            for neighbour in edges.get(table, ()):
                if neighbour not in seen and len(seen) < limit:
                    seen.add(neighbour)
                    nxt.add(neighbour)
        frontier = nxt
    return seen


# --------------------------------------------------------------------------------------
# Graph formats
# --------------------------------------------------------------------------------------


def to_dot(ontology: Ontology, tables: set[str], title: str = "") -> str:
    """Graphviz. `dot -Tsvg` turns this into the picture."""
    class_of = {c.table: c.label for c in ontology.classes}
    conf = {c.table: c.confidence for c in ontology.classes}
    props: dict[str, int] = {}
    for p in ontology.properties:
        props[p.table] = props.get(p.table, 0) + 1

    out = [
        "digraph ontology {",
        '  graph [rankdir=LR, fontname="Helvetica", labelloc=t, '
        f'label="{title}", fontsize=18, splines=true, overlap=false];',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11];',
        '  edge [fontname="Helvetica", fontsize=9, color="#64748b"];',
    ]
    for table in sorted(tables):
        label = class_of.get(table, table)
        c = conf.get(table)
        # Confidence colours the node, so the weak parts of the ontology are visible at a glance.
        fill = "#dbeafe" if c is None or c >= 0.9 else ("#fef3c7" if c >= 0.7 else "#fee2e2")
        line = "#1d4ed8" if c is None or c >= 0.9 else ("#b45309" if c >= 0.7 else "#b91c1c")
        detail = f"\\n{props.get(table, 0)} properties" if props.get(table) else ""
        conf_text = f"\\n{c:.2f}" if c is not None else ""
        out.append(
            f'  "{table}" [label="{label}{detail}{conf_text}", fillcolor="{fill}", color="{line}"];'
        )
    # The taxonomy, which used to be missing from the picture entirely: the ontology carried
    # rdfs:subClassOf into the Turtle, but nothing drew it, so a reader of the image concluded
    # there was no hierarchy at all. Half the parents (Person, Organisation, Address) are
    # abstract -- no table maps to them -- so they need nodes of their own to be drawn.
    # A parent is named by label, and labels are not unique -- thirteen of them are shared by
    # more than one table in a real Odoo, Product among them. Resolving a parent to whichever
    # table happened to be last drew ProductVariant is-a stock_route_product. So a parent
    # becomes an edge only when exactly one candidate is being drawn; otherwise it is shown as
    # an abstract node, which says what is known without asserting what is not.
    tables_of_label: dict[str, list[str]] = {}
    for c in ontology.classes:
        tables_of_label.setdefault(c.label, []).append(c.table)
    abstract: set[str] = set()
    is_a: list[tuple[str, str]] = []
    for c in ontology.classes:
        if c.table not in tables or not c.parent:
            continue
        candidates = [t for t in tables_of_label.get(c.parent, []) if t in tables and t != c.table]
        if len(candidates) == 1:
            is_a.append((c.table, candidates[0]))
        elif not tables_of_label.get(c.parent):
            abstract.add(c.parent)
            is_a.append((c.table, f"abstract::{c.parent}"))
    for label in sorted(abstract):
        out.append(
            f'  "abstract::{label}" [label="{label}", fillcolor="#f8fafc", '
            'color="#94a3b8", style="rounded,filled,dashed"];'
        )
    for child, parent in is_a:
        # Hollow arrowhead, the usual way an is-a reads apart from an association.
        out.append(
            f'  "{child}" -> "{parent}" '
            '[label="is-a", arrowhead=onormal, style=dashed, color="#475569"];'
        )

    for r in ontology.relations:
        if r.from_table in tables and r.to_table in tables:
            out.append(f'  "{r.from_table}" -> "{r.to_table}" [label="{r.label}"];')
    out.append("}")
    return "\n".join(out)


def to_graphml(ontology: Ontology, tables: set[str]) -> str:
    """GraphML for Gephi / yEd / Cytoscape."""
    class_of = {c.table: c.label for c in ontology.classes}
    conf = {c.table: c.confidence for c in ontology.classes}
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '  <key id="label" for="node" attr.name="label" attr.type="string"/>',
        '  <key id="table" for="node" attr.name="table" attr.type="string"/>',
        '  <key id="confidence" for="node" attr.name="confidence" attr.type="double"/>',
        '  <key id="relation" for="edge" attr.name="relation" attr.type="string"/>',
        '  <graph id="ontology" edgedefault="directed">',
    ]
    for table in sorted(tables):
        out.append(f'    <node id="{escape(table)}">')
        out.append(f'      <data key="label">{escape(class_of.get(table, table))}</data>')
        out.append(f'      <data key="table">{escape(table)}</data>')
        if conf.get(table) is not None:
            out.append(f'      <data key="confidence">{conf[table]}</data>')
        out.append("    </node>")
    for i, r in enumerate(ontology.relations):
        if r.from_table in tables and r.to_table in tables:
            out.append(
                f'    <edge id="e{i}" source="{escape(r.from_table)}" target="{escape(r.to_table)}">'
                f'<data key="relation">{escape(r.label)}</data></edge>'
            )
    out.extend(["  </graph>", "</graphml>"])
    return "\n".join(out)


def to_mermaid(ontology: Ontology, tables: set[str]) -> str:
    """A fenced mermaid block, which renders in markdown and on GitHub."""
    class_of = {c.table: c.label for c in ontology.classes}
    out = ["```mermaid", "graph LR"]
    for table in sorted(tables):
        out.append(f'    {_iri(table)}["{class_of.get(table, table)}"]')
    for r in ontology.relations:
        if r.from_table in tables and r.to_table in tables:
            out.append(f"    {_iri(r.from_table)} -->|{r.label}| {_iri(r.to_table)}")
    out.append("```")
    return "\n".join(out)
