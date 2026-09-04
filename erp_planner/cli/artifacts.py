"""Commands that stand alone: export the ontology, and walk the guided pipeline."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from erp_planner.cli.common import _read, console
from erp_planner.models import Ontology


def cmd_export(
    ontology: Path = typer.Argument(..., help="the mapped ontology"),
    out: Path = typer.Option(..., "--out", "-o", help="output file; the suffix picks the format"),
    focus: str = typer.Option(None, "--focus", help="a concept to centre the graph on"),
    depth: int = typer.Option(1, "--depth", help="how many hops out from --focus"),
    limit: int = typer.Option(40, "--limit", help="most nodes to draw"),
    render: bool = typer.Option(True, "--render/--no-render", help="run graphviz on a .dot file"),
) -> None:
    """Export the ontology for another tool to open.

    `.ttl` OWL/Turtle (Protege, WebVOWL) · `.dot` Graphviz, rendered to SVG if `dot` is installed
    · `.graphml` Gephi/yEd · `.md` mermaid.

    Graph formats draw a *neighbourhood*: 411 concepts in one picture is not a picture.
    """
    import shutil
    import subprocess

    from erp_planner.serve.export import (
        neighbourhood,
        to_dot,
        to_graphml,
        to_mermaid,
        to_turtle,
    )

    model = _read(Ontology, ontology)
    suffix = out.suffix.lower()
    out.parent.mkdir(parents=True, exist_ok=True)

    if suffix in {".ttl", ".owl"}:
        out.write_text(to_turtle(model))
        console.print(
            f"[green]wrote[/green] {out}  ·  {len(model.classes)} classes, "
            f"{len(model.properties)} datatype properties, {len(model.relations)} object properties"
        )
        console.print("[dim]open it in Protégé, or drop it into webvowl.org for a graph[/dim]")
        return

    tables = neighbourhood(model, focus, depth, limit)
    if not tables:
        console.print(f"[red]no concept named {focus!r}[/red] in this ontology")
        raise typer.Exit(1)
    title = f"{focus} — {depth} hop(s)" if focus else f"{len(tables)} most connected concepts"

    if suffix == ".graphml":
        out.write_text(to_graphml(model, tables))
    elif suffix in {".md", ".mmd"}:
        out.write_text(to_mermaid(model, tables))
    else:
        out.write_text(to_dot(model, tables, title))
    console.print(f"[green]wrote[/green] {out}  ·  {len(tables)} concepts")

    if suffix == ".dot" and render:
        if shutil.which("dot") is None:
            console.print("[dim]install graphviz to render this to an image[/dim]")
            return
        for fmt in ("svg", "png"):
            image = out.with_suffix(f".{fmt}")
            subprocess.run(["dot", f"-T{fmt}", str(out), "-o", str(image)], check=False)
            console.print(f"[green]rendered[/green] {image}")

def cmd_pipeline(
    yes: bool = typer.Option(False, "--yes", "-y", help="run every step without asking"),
    only: list[str] = typer.Option(None, "--only", help="run only these steps, by key"),
) -> None:
    """Walk the whole pipeline, one step at a time, asking before each.

    Every step says what it does, what it reads and writes, and what it costs. Nothing runs until
    you accept it. Steps whose inputs are missing are skipped rather than failing.
    """
    from erp_planner.pipeline import (
        DEMO_DB,
        DEMO_ODOO_URL,
        choose_provider,
        connect,
        default_steps,
        ensure_api_key,
        run,
    )

    # Work out what to connect to before showing a wall of somebody else's connection string.
    connection = None
    if not only or "ingest" in set(only):
        connection = connect(
            console,
            os.environ.get("ODOO_DB", DEMO_DB),
            os.environ.get("ODOO_URL", DEMO_ODOO_URL),
        )
        if connection is None:
            console.print("[dim]no connection; nothing to ingest.[/dim]")
            raise typer.Exit(1)

    # Which provider, before which key - the key that matters depends on the answer.
    provider = choose_provider(console)
    every = default_steps(connection)
    steps = every
    # Check the key before any step runs: without one the paid steps fail, and finding that out
    # after ingest and plan have already run is several wasted minutes.
    if any(s.costs_money for s in steps) and not ensure_api_key(console, provider):
        steps = [s for s in steps if not s.costs_money]
    if only:
        wanted = set(only)
        steps = [s for s in steps if s.key in wanted]
        if not steps:
            console.print(f"[red]no steps named[/red] {', '.join(only)}")
            raise typer.Exit(2)

    paid = [s for s in steps if s.costs_money]
    console.print(
        f"[bold]{len(steps)} steps[/bold] · "
        + (f"[red]{len(paid)} cost money[/red] ({', '.join(s.cost for s in paid)})"
           if paid else "all free")
    )
    ran = run(steps, console=console, assume_yes=yes, known=every)
    console.print(f"[bold]{ran}/{len(steps)}[/bold] steps ran.")
