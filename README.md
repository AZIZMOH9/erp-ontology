# erp-ontology

**Turn an ERP database into an ontology.**

An ERP records structure — tables, columns, foreign keys — but not meaning. SAP's `KNA1` is the
customer master; nothing in the database says so. Odoo's `x_sup_qual_rec`, with columns called
`x_prt` and `x_scr`, was built by a consultant who left years ago. That missing layer is why AI
agents can't safely operate on ERP data, why merging two systems is brutal, and why ontology
projects still need specialists.

This reads the schema, works out what each table and column means in business terms, says which of
those conclusions to trust, and writes the result as OWL.

```turtle
:ScrapEvent a owl:Class ;                    # from x_mfg_scrap — a table nobody documented
    rdfs:label "ScrapEvent" .
:scrappedQuantity a owl:DatatypeProperty ;
    rdfs:domain :ScrapEvent ; rdfs:range xsd:decimal .
:Employee a owl:Class ;
    rdfs:subClassOf :Person .
```

On a 418-table Odoo it produces **418 classes, 1,775 properties and 1,054 relations**, each
traceable to the table it came from, in about five minutes and roughly a dollar of model calls.

## Where the code is

The system lives on the [**`dev`**](../../tree/dev) branch — the package, the CLI, the docs, and
everything needed to run it against your own ERP. This branch is the front page only.

```bash
git clone -b dev https://github.com/AZIZMOH9/erp-ontology.git
```

## Status

Working and measured, not finished. Mapping accuracy has been evaluated against RODI, an external
gold standard, and end to end by question answering. Two things are known to be weak: the review
queue's catch rate sits below its target, and the class hierarchy is thin — 16 of 418 classes carry
a parent. Both are recorded in the docs on `dev` rather than papered over.

MIT licensed.
