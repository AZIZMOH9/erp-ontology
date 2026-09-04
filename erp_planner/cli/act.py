"""Phase 6 commands. There is no execute path; see erp_planner.act.actions."""

from __future__ import annotations

from pathlib import Path

import typer

from erp_planner.cli.common import _read, console
from erp_planner.models import Ontology

act_app = typer.Typer(help="Phase 6 - plan ERP actions. Dry run only.", no_args_is_help=True)


@act_app.command("plan")
def cmd_act_plan(
    concept: str = typer.Argument(..., help="business concept, e.g. SalesOrder"),
    ontology: Path = typer.Option(..., "--ontology", "-o"),
    kind: str = typer.Option("write", "--kind", help="create | write | reserve"),
    record_id: int = typer.Option(None, "--id", help="record to act on"),
    set_: list[str] = typer.Option(None, "--set", help="field=value, repeatable"),
    verification: Path = typer.Option(None, "--verification", help="a verify run, to block flagged mappings"),
    allow: list[str] = typer.Option(None, "--allow", help="concepts permitted to be written"),
) -> None:
    """Show the ERP call an action WOULD make. Nothing is ever sent.

    There is no --execute. The capability was not written: on a 50% catch rate for wrong mappings,
    and with no mapping in this system yet reviewed by a human, a plan is the product.
    """
    from erp_planner.act import Action, ActionKind, plan
    from erp_planner.verify.verifier import VerificationReport

    values = dict(pair.split("=", 1) for pair in (set_ or []) if "=" in pair)
    report = _read(VerificationReport, verification) if verification else None
    result = plan(
        Action(kind=ActionKind(kind), concept=concept, values=values, record_id=record_id),
        _read(Ontology, ontology), report, set(allow) if allow else None,
    )
    colour = {"blocked": "red", "review": "yellow", "routine": "green"}[result.risk.value]
    console.print(f"[{colour}]{result.render()}[/{colour}]")
    raise typer.Exit(0 if result.call else 1)
