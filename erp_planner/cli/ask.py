"""Phase 5 commands: ask a question, and score answers against reference queries."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table as RichTable

from erp_planner.cli.common import _read, _runner, console
from erp_planner.llm.providers import Provider
from erp_planner.models import Ontology, SchemaSnapshot

ask_app = typer.Typer(help="Phase 5 - ask the ERP questions through the ontology.", no_args_is_help=True)



@ask_app.command("question")
def cmd_ask(
    question: str = typer.Argument(..., help="a question in plain English"),
    ontology: Path = typer.Option(..., "--ontology", "-o", help="the mapped ontology"),
    schema: Path = typer.Option(..., "--schema", "-s", help="the schema snapshot"),
    db_url: str = typer.Option(..., "--db-url", envvar="ERP_PLANNER_DB_URL"),
    db_schema: str = typer.Option("public", "--db-schema"),
    provider: Provider = typer.Option(
        Provider.ANTHROPIC, "--provider", envvar="ERP_PLANNER_PROVIDER"
    ),
    model: str = typer.Option(None, "--model"),
    api_key: str = typer.Option(None, "--api-key"),
    show_sql: bool = typer.Option(True, "--sql/--no-sql"),
) -> None:
    """Answer one question against the live ERP. Read-only: writes are refused before they run."""
    from sqlalchemy import create_engine

    from erp_planner.serve.query import ask

    result = ask(
        _runner(provider, model, api_key), question,
        _read(Ontology, ontology), _read(SchemaSnapshot, schema),
        create_engine(db_url), schema=db_schema,
    )
    if result.unanswerable:
        console.print(f"[yellow]cannot answer:[/yellow] {result.unanswerable}")
        raise typer.Exit(1)
    if show_sql and result.sql:
        console.print(f"[dim]{result.sql}[/dim]")
    if result.error:
        console.print(f"[red]{result.error}[/red]")
        raise typer.Exit(1)
    table = RichTable(header_style="bold")
    for column in result.columns:
        table.add_column(column)
    for row in result.rows[:25]:
        table.add_row(*row)
    console.print(table)
    if len(result.rows) > 25:
        console.print(f"[dim]... {len(result.rows) - 25} more rows[/dim]")

