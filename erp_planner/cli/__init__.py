"""The command-line interface, one module per phase.

    ingest      Phase 1   read an ERP, remask a snapshot
    mapping     Phase 2   plan the routing, map the schema
    verify      Phase 3   score mappings, produce the review queue
    ask         Phase 5   ask a question through the ontology
    act         Phase 6   plan an action; there is no execute path
    artifacts             export the ontology, walk the guided pipeline

The benchmark harness is not here. Obfuscating a schema and scoring against a gold standard are
things done *to* this system, not with it, so they live in `benchmarks/` at the repo root and are
not shipped: they were a quarter of the installed package, for code no user could reach.

Split by phase because a 778-line module with fifteen commands cannot be reviewed. Per spec §6 the
CLI stays a thin interface over the engine: each command parses JSON into models, calls one engine
function, and renders.
"""

from __future__ import annotations

import typer
from dotenv import load_dotenv

from erp_planner.cli import act, artifacts, ask, ingest, mapping, verify
from erp_planner.cli.common import console

# Read .env before Typer resolves envvar-backed options, so a key in .env behaves like an
# exported one. Never printed, and .env is gitignored.
load_dotenv()

app = typer.Typer(help="Agentic ERP-to-Ontology system.", no_args_is_help=True)
app.add_typer(ingest.ingest_app, name="ingest")
app.add_typer(mapping.map_app, name="map")
app.add_typer(verify.verify_app, name="verify")
app.add_typer(ask.ask_app, name="ask")
app.add_typer(act.act_app, name="act")

# Commands that are not part of a phase group.
app.command("export")(artifacts.cmd_export)
app.command("pipeline")(artifacts.cmd_pipeline)

__all__ = ["app", "console"]


if __name__ == "__main__":
    app()
