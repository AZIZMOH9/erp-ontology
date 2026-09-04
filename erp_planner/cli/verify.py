"""Phase 3 commands: score every mapping, and measure the flags against a gold standard."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table as RichTable

from erp_planner.cli.common import _read, _runner, _write, console
from erp_planner.llm.providers import Provider
from erp_planner.models import Ontology, SchemaSnapshot
from erp_planner.verify.verifier import (
    DEFAULT_FLAG_THRESHOLD,
    VerificationReport,
    verify,
)

verify_app = typer.Typer(help="Phase 3 - decide which mappings can be trusted.", no_args_is_help=True)


def _verify(ontology_path, schema_path, against, threshold):
    ontology = _read(Ontology, ontology_path)
    snapshot = _read(SchemaSnapshot, schema_path)
    others = [_read(Ontology, p) for p in (against or [])]
    return ontology, verify(ontology, snapshot, against=others, threshold=threshold), others

@verify_app.command("judge")
def cmd_verify_judge(
    ontology: Path = typer.Argument(..., help="the mapped ontology"),
    schema: Path = typer.Option(..., "--schema", "-s"),
    against: Path = typer.Option(..., "--against", "-a", help="an independent second run"),
    queue: Path = typer.Option(None, "--queue", "-q", help="a review queue to narrow in place"),
    provider: Provider = typer.Option(Provider.ANTHROPIC, "--provider"),
    model: str = typer.Option(None, "--model"),
    api_key: str = typer.Option(None, "--api-key"),
    threshold: float = typer.Option(DEFAULT_FLAG_THRESHOLD, "--threshold"),
    sample: int = typer.Option(
        None, "--sample", help="judge this many at random and report, without changing the queue"
    ),
    out: Path = typer.Option(None, "--out", "-o"),
) -> None:
    """Drop the flagged mappings whose two runs merely worded it differently.

    Flagging catches disagreements, but most disagreements are not about meaning -- measured at
    95% wording on a real schema. Judging them turns a queue nobody would work through into one
    somebody might.
    """
    from erp_planner.verify.narrow import narrow

    mapped = _read(Ontology, ontology)
    snapshot = _read(SchemaSnapshot, schema)
    other = _read(Ontology, against)
    report = (
        _read(VerificationReport, queue)
        if queue and queue.exists()
        else verify(mapped, snapshot, against=[other], threshold=threshold)
    )

    runner = _runner(provider, model, api_key)
    result = narrow(report, mapped, other, snapshot, runner, sample=sample)

    table = RichTable(title="Judging the disagreements", header_style="bold")
    table.add_column("measure")
    table.add_column("value", justify="right")
    table.add_row("flagged before", str(result.flagged_before))
    table.add_row("disagreements judged", str(result.compared))
    table.add_row("  same meaning, different words", f"[green]{result.same_meaning}[/green]")
    table.add_row("  genuinely different", f"[red]{result.different_meaning}[/red]")
    table.add_row("  unclear", str(result.unclear))
    if sample:
        table.add_row("sampled", f"{result.wording_share:.0%} wording — queue unchanged")
    else:
        table.add_row("flagged after", f"[bold]{result.flagged_after}[/bold]")
        load = result.flagged_after / len(report.verdicts) if report.verdicts else 0
        colour = "green" if load <= 0.20 else "red"
        table.add_row("review load", f"[{colour}]{load:.0%}[/{colour}] (ceiling 20%)")
    console.print(table)
    console.print(f"[dim]{runner.usage.calls} calls, ${runner.usage.cost_usd:.2f}[/dim]")

    if out:
        _write(report, out)
        console.print(f"[green]wrote[/green] {out}")


@verify_app.command("run")
def cmd_verify_run(
    ontology: Path = typer.Argument(..., help="the mapped ontology"),
    schema: Path = typer.Option(..., "--schema", "-s", help="the schema it was mapped from"),
    against: list[Path] = typer.Option(
        None, "--against", "-a", help="independent run(s) of the same schema; repeatable"
    ),
    threshold: float = typer.Option(DEFAULT_FLAG_THRESHOLD, "--threshold"),
    out: Path = typer.Option(None, "--out", "-o", help="write the review queue here"),
    show: int = typer.Option(15, "--show"),
) -> None:
    """Score every mapping on evidence independent of the model, and flag what a human should see."""
    _, report, others = _verify(ontology, schema, against, threshold)
    if not others:
        console.print(
            "[yellow]no --against run given[/yellow]: independent agreement is the strongest "
            "signal available, and without it the catch rate roughly halves."
        )

    console.print(
        f"[bold]{len(report.verdicts)}[/bold] mappings  ·  "
        f"[yellow]{len(report.flagged)}[/yellow] flagged  ·  "
        f"review load [bold]{report.review_load:.0%}[/bold] (ceiling 20%)"
    )
    table = RichTable(title="Review queue — least trusted first", header_style="bold")
    for col in ("trust", "kind", "key", "label", "why"):
        table.add_column(col, justify="right" if col == "trust" else "left")
    for verdict in report.queue()[:show]:
        table.add_row(
            f"{verdict.trust:.2f}", verdict.kind.value, verdict.key, verdict.label, verdict.explain()
        )
    console.print(table)
    if len(report.flagged) > show:
        console.print(f"[dim]... {len(report.flagged) - show} more flagged[/dim]")
    if out:
        _write(report, out)
        console.print(f"[green]wrote[/green] {out}")

