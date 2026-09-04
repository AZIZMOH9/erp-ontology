# erp-planner

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

**Turn an ERP database into an ontology.** Point it at your schema; it works out what every table
and column means, writes the result as OWL, tells you which parts to trust, and answers questions
through it in plain English.

It runs on your machine against a PostgreSQL-backed ERP — developed against Odoo — and needs one
API key, from Anthropic, Google or OpenAI. You walk the pipeline a step at a time, accepting or
rejecting each one, and it leaves the ontology in `erp-planner/` as `ontology.json` and
`ontology.ttl`.

---

## What this is

An ERP database records **structure** — tables, columns, foreign keys — but not **meaning**. SAP's
`KNA1` is the customer master; nothing in the database says so. Odoo's `x_sup_qual_rec`, with
columns called `x_prt` and `x_scr`, was built by a consultant who left in 2014.

This builds the missing layer. It connects to your ERP, reads the schema, and works out what each
table and column means in business terms — producing an **ontology**: named business concepts, the
attributes they carry, and the relationships between them, each traceable back to the table it came
from. Then it tells you which of those conclusions to trust, and lets you ask the database
questions without knowing any of the table names.

Your data does not leave the machine except in the model call you configure, and sample values are
masked before even that.

## What it does

| Step | | Writes | Cost |
|---|---|---|---|
| **ingest** | reads your schema: tables, columns, keys, sample values | `schema.json` | free |
| **plan** | shows how it will split the work and what that will cost | — | free |
| **map** | **builds the ontology** | **`ontology.json`** | ~$1 / 400 tables |
| **second** | maps it again, independently, so disagreements can be spotted | `second.json` | ~$0.75 |
| **verify** | ranks every mapping by how much it can be trusted | `queue.json` | free |
| **judge** | drops flagged mappings that are only *worded* differently | `queue.json` | ~$0.20 |
| **export** | **the ontology as OWL/Turtle** — Protégé, WebVOWL | **`ontology.ttl`** | free |
| **graph** | draws it | `graph.svg` | free |
| **ask** | answers questions through the ontology | — | pennies |
| **act** | shows the ERP call an action *would* make. Dry run only | — | free |

Everything lands in `./erp-planner/`.

![The pipeline](docs/workflow.svg)

## What you get

The ontology is the point. On a 418-table Odoo it comes out as **411 classes, 1,637 properties and
958 relations**, each with a confidence score:

```jsonc
// erp-planner/ontology.json
{ "classes": [
    { "table": "product_template", "label": "Product",     "confidence": 1.00 },
    { "table": "x_mfg_scrap",      "label": "ScrapEvent",  "confidence": 0.85,
      "rationale": "a quantity and a reason code, keyed to a product" }],
  "properties": [
    { "table": "x_mfg_scrap", "column": "x_qty", "label": "scrappedQuantity", "datatype": "xsd:decimal" }],
  "relations": [
    { "from_table": "x_mfg_scrap", "from_columns": ["x_tmpl"],
      "to_table": "product_template", "label": "scrappedProduct" }] }
```

`x_mfg_scrap` is a table nobody documented. The system read its columns, its foreign key to
`product_template` and its values, and concluded it records material scrapped during production.

The same thing as OWL, which opens in Protégé or [WebVOWL](https://service.tib.eu/webvowl):

```turtle
# erp-planner/ontology.ttl
:ScrapEvent a owl:Class ;
    rdfs:label "ScrapEvent" ;
    rdfs:comment "table: x_mfg_scrap" .

:scrappedQuantity a owl:DatatypeProperty ;
    rdfs:domain :ScrapEvent ;
    rdfs:range xsd:decimal .

:scrappedProduct a owl:ObjectProperty ;
    rdfs:domain :ScrapEvent ;
    rdfs:range :Product .
```

## Getting started

```bash
mise install        # Python 3.12 and uv
mise run setup      # install the tool
mise run pipeline   # everything else
```

`pipeline` asks for a database, a model provider (Claude, Gemini or GPT) and an API key, remembers
them in a gitignored `.env`, then walks each step — showing what it does, what it costs and how
long it takes — and waits for you before running anything.

**No ERP to try it against?** `mise run demo:up && mise run demo:seed` gives you a throwaway
Odoo 17 with data in it.

Results land in `./erp-planner/`. Nothing is written into the tool itself.

### Running one step at a time

```bash
mise run ingest                     # free
mise run plan                       # free — see the cost before paying it
mise run map                        # costs money
mise run map:second                 # a second opinion; doubles what verify can catch
mise run verify                     # free — the review queue
mise run judge                      # narrows the queue to real disagreements
mise run ask "which products have no internal reference"
mise run export                     # OWL/Turtle
mise run graph ScrapEvent           # an image of one concept's neighbourhood
```

`mise tasks` lists everything.

### Configuration

Precedence is **flag → environment variable → `.env` → prompt**, and the prompt only fires when
there's a terminal, so nothing hangs in CI.

```bash
ODOO_DB=postgresql://user:password@host:5432/database
ERP_PLANNER_PROVIDER=anthropic          # or google, openai
ANTHROPIC_API_KEY=...
ERP_PLANNER_HOME=/where/results/go      # defaults to ./erp-planner
```

## Repository structure

```
erp_planner/            the tool
  cli/                  the command line, one module per step
  llm/                  how it talks to a model
    providers.py          Claude, Gemini or GPT behind one interface
    runner.py             where calls happen; tokens and cost tracked here
    prompts.py            every instruction a model is given, in one file
  ingest/               reading your ERP
    odoo.py               Postgres introspection plus Odoo's own field metadata
  mapping/              working out what the schema means
    hardness.py           decides which tables need the expensive treatment
    orchestrator.py       routes and collects; never calls a model itself
    llm/                  the model-facing half
      base_llm.py           one call per group of tables — most of the work
      agent.py              investigates with tools, for the hard ones
      tools.py              what it may ask your database
      reconcile.py          merges duplicate concepts
  verify/               deciding what to trust
    signals.py            evidence independent of the model's own opinion
    judge.py              do two answers mean the same thing?
  serve/                asking questions
    query.py              question → SQL, read-only and enforced
    export.py             OWL, Graphviz, GraphML, mermaid
  act/                  actions — planned, never executed
  clustering.py         splits the schema into work units
  masking.py            hides values before they leave your network
  vocabulary.py         business concepts and their synonyms

docker/                 a throwaway Odoo, only if you have none to try against
docs/                   how it works, and what it does with your data
mise.toml               every command above
```

## What it does with your data

**It runs on your machine.** There is no hosted service. The only thing leaving your network is the
model call you configured, carrying a description of your schema — table and column names, types,
keys, and a handful of sample values per column.

**Sample values are masked by default.** `ACME GmbH` becomes `AAAA AaaA`, `DE811907980` becomes
`AA999999999`. Shape and distribution are what the model needs; the characters are not. Every run
prints exactly what was masked before anything is sent.

**Your database is read-only to it.** Ingestion issues `SELECT` statements. Questions generate
`SELECT` statements, and anything that is not a single read is refused *before* it runs — asking a
model not to write is not a control.

**It cannot write to your ERP.** The action planner shows the API call it *would* make and stops.
There is no flag to change that.

[docs/data-handling.md](docs/data-handling.md) has the detail — it is written to be shown to
whoever has to approve this.

## How it works

[docs/workflow.md](docs/workflow.md) — the pipeline, with real input and output at each step.
[docs/agentic-flow.md](docs/agentic-flow.md) — the part that investigates: when it runs, the four
tools it can use, and what they are measurably worth.

Most tables do not need investigation. A deterministic score decides which do, and on a 418-table
Odoo only 2 of 77 groups took the expensive path.

## What it is not

**It is not accurate enough to trust unreviewed.** On an external benchmark it maps meaning
correctly about **98%** of the time when names are readable, and about **71%** when every
identifier is hidden — but it can only tell you *which* mappings are wrong about **half** the time.
Treat the review queue as a starting point, not a verdict.

**It does not write to your ERP**, deliberately, for the same reason. A mapping that is 98% right
still means writing to the wrong table 2% of the time, and an ERP does not survive that quietly.

**It has been tested on Odoo.** A plain Postgres connection works against anything and gets you
structure without field labels, but nothing else has been tried end to end.

## Requirements

- Python 3.12 and [mise](https://mise.jdx.dev)
- A read-only connection to your ERP's database
- An API key from Anthropic, Google or OpenAI
- Docker, only for the demo

## License

[MIT](LICENSE).
