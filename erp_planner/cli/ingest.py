"""Phase 1 commands: read an ERP, and tighten the masking on a snapshot."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table as RichTable

from erp_planner.cli.common import _read, _write, console
from erp_planner.ingest.odoo import DEFAULT_EXCLUDE_PREFIXES, ingest_odoo
from erp_planner.masking import MaskingMode, MaskingReport, apply_masking
from erp_planner.models import SchemaSnapshot

ingest_app = typer.Typer(help="Phase 1 - read an ERP into a schema snapshot.", no_args_is_help=True)


def _render_masking(report: MaskingReport, show_raw: int = 15) -> None:
    """Show exactly what would leave the database. This is the trust conversation, in a table."""
    table = RichTable(title=f"Masking - mode: {report.mode.value}", header_style="bold")
    table.add_column("measure")
    table.add_column("count", justify="right")
    table.add_row("columns", str(report.columns_total))
    table.add_row("columns with sample values", str(report.columns_with_samples))
    table.add_row("masked", f"[green]{report.columns_masked}[/green]")
    table.add_row(
        "sent raw",
        f"[red]{report.columns_raw}[/red]" if report.columns_raw else "0",
    )
    for category, count in sorted(report.by_category.items(), key=lambda kv: -kv[1]):
        table.add_row(f"  masked as {category}", str(count))
    console.print(table)
    if report.raw_columns:
        shown = ", ".join(report.raw_columns[:show_raw])
        more = f" (+{len(report.raw_columns) - show_raw} more)" if len(report.raw_columns) > show_raw else ""
        console.print(f"[yellow]raw columns:[/yellow] {shown}{more}")
        console.print(
            "[dim]Spot-check these before sending. In 'sensitive' mode the promise is only as "
            "good as the classifier.[/dim]"
        )

@ingest_app.command("odoo")
def cmd_ingest_odoo(
    db_url: str = typer.Option(..., "--db-url", envvar="ERP_PLANNER_DB_URL", help="read-only Postgres URL"),
    out: Path = typer.Option(..., "--out", "-o", help="where to write the schema snapshot"),
    schema: str = typer.Option("public", "--schema"),
    odoo_url: str = typer.Option(None, "--odoo-url", envvar="ERP_PLANNER_ODOO_URL"),
    odoo_db: str = typer.Option(None, "--odoo-db", envvar="ERP_PLANNER_ODOO_DB"),
    odoo_user: str = typer.Option(None, "--odoo-user", envvar="ERP_PLANNER_ODOO_USER"),
    odoo_password: str = typer.Option(None, "--odoo-password", envvar="ERP_PLANNER_ODOO_PASSWORD"),
    masking: MaskingMode = typer.Option(MaskingMode.ALL, "--masking", "-m", help="all | sensitive | none"),
    sample_rows: int = typer.Option(200, "--sample-rows", help="rows read per table"),
    values_per_column: int = typer.Option(5, "--values-per-column"),
    max_tables: int = typer.Option(None, "--max-tables", help="cap, for a quick look"),
    include_all: bool = typer.Option(False, "--include-all", help="do not skip Odoo's own plumbing tables"),
) -> None:
    """Read an Odoo database into a schema snapshot.

    Without --odoo-url this reads Postgres alone: structure survives, but field labels and
    reliable custom-model detection do not.
    """
    snapshot = ingest_odoo(
        db_url,
        schema=schema,
        odoo_url=odoo_url,
        odoo_db=odoo_db,
        odoo_user=odoo_user,
        odoo_password=odoo_password,
        sample_rows=sample_rows,
        values_per_column=values_per_column,
        exclude_prefixes=() if include_all else DEFAULT_EXCLUDE_PREFIXES,
        max_tables=max_tables,
    )
    report = apply_masking(snapshot, masking)
    _write(snapshot, out)

    tiers: dict[str, int] = {}
    for t in snapshot.tables:
        tiers[t.tier.value] = tiers.get(t.tier.value, 0) + 1
    console.print(
        f"[green]wrote[/green] {out}\n"
        f"tables={len(snapshot.tables)}  columns={sum(len(t.columns) for t in snapshot.tables)}  "
        f"foreign_keys={sum(len(t.foreign_keys) for t in snapshot.tables)}  tiers={tiers}"
    )
    _render_masking(report)

@ingest_app.command("remask")
def cmd_remask(
    snapshot_path: Path = typer.Argument(..., help="schema snapshot JSON"),
    mode: MaskingMode = typer.Option(..., "--mode", "-m", help="all | sensitive"),
    out: Path = typer.Option(None, "--out", "-o", help="defaults to overwriting in place"),
) -> None:
    """Tighten the masking on an existing snapshot.

    Masking is one-way, so this can only make a snapshot safer, never restore raw values. Re-run
    `ingest odoo` to get those back.
    """
    if mode is MaskingMode.NONE:
        console.print("[red]masking cannot be undone[/red] - re-run `ingest odoo` instead.")
        raise typer.Exit(2)
    snapshot = _read(SchemaSnapshot, snapshot_path)
    report = apply_masking(snapshot, mode)
    _write(snapshot, out or snapshot_path)
    console.print(f"[green]wrote[/green] {out or snapshot_path}")
    _render_masking(report)
