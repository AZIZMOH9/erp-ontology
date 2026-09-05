"""Phase 2 commands: see the routing for free, then map the schema."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table as RichTable

from erp_planner.cli.common import _read, _write, console
from erp_planner.clustering import cluster_schema
from erp_planner.llm.providers import API_KEY_ENV, Provider, api_key_for, cost_is_known
from erp_planner.mapping.hardness import DEFAULT_THRESHOLD
from erp_planner.mapping.hardness import score as hardness_score
from erp_planner.mapping.llm.rendering import build_prefix
from erp_planner.mapping.orchestrator import (
    ExecutionMode,
    Orchestrator,
    OrchestratorConfig,
)
from erp_planner.mapping.orchestrator import Path as MapPath
from erp_planner.models import SchemaSnapshot

map_app = typer.Typer(help="Phase 2 - infer what the schema means.", no_args_is_help=True)


@map_app.command("plan")
def cmd_map_plan(
    schema: Path = typer.Argument(..., help="schema snapshot JSON"),
    max_tables: int = typer.Option(12, "--max-tables"),
    max_columns: int = typer.Option(150, "--max-columns"),
    threshold: float = typer.Option(DEFAULT_THRESHOLD, "--threshold", help="agent-path cutoff"),
    show: int = typer.Option(12, "--show", help="clusters to list"),
) -> None:
    """Show the split, the hardness scores and the routing — before spending anything.

    Routing is a pure function of the snapshot, so this is exactly what `map run` will do.
    """
    snapshot = _read(SchemaSnapshot, schema)
    clusters = cluster_schema(snapshot, max_tables=max_tables, max_columns=max_columns)
    scored = [(hardness_score(c), c) for c in clusters]
    agent = [(h, c) for h, c in scored if h.score >= threshold]
    prefix = build_prefix(snapshot)

    console.print(
        f"[bold]{len(clusters)}[/bold] clusters over {len(snapshot.tables)} tables  ·  "
        f"[yellow]{len(agent)}[/yellow] on the agent path (hardness >= {threshold}), "
        f"{len(clusters) - len(agent)} on the base path"
    )
    console.print(
        f"cached prefix: ~{len(prefix) // 4:,} tokens  ·  "
        f"reused across every call [dim](Anthropic only)[/dim]"
    )

    table = RichTable(header_style="bold")
    numeric = {"#", "hard", "cust", "undoc", "isol", "opaq"}
    for col in ("#", "hard", "cust", "undoc", "isol", "opaq", "path", "driver", "why"):
        table.add_column(col, justify="right" if col in numeric else "left")
    for h, cluster in sorted(scored, key=lambda pair: -pair[0].score)[:show]:
        on_agent = h.score >= threshold
        table.add_row(
            str(cluster.index),
            f"{h.score:.3f}",
            f"{h.custom:.2f}",
            f"{h.undocumented:.2f}",
            f"{h.isolation:.2f}",
            f"{h.opacity:.2f}",
            "[yellow]agent[/yellow]" if on_agent else "base",
            h.driver or "",
            h.explain(),
        )
    console.print(table)
    if len(clusters) > show:
        console.print(f"[dim]... {len(clusters) - show} more, all on the base path[/dim]")

@map_app.command("run")
def cmd_map_run(
    schema: Path = typer.Argument(..., help="schema snapshot JSON"),
    out: Path = typer.Option(..., "--out", "-o", help="where to write the ontology"),
    provider: Provider = typer.Option(
        Provider.ANTHROPIC,
        "--provider",
        envvar="ERP_PLANNER_PROVIDER",
        help="anthropic | openai | google | openrouter",
    ),
    model: str = typer.Option(None, "--model", help="bulk model (default: provider's)"),
    hard_model: str = typer.Option(None, "--hard-model", help="model for the agent path"),
    api_key: str = typer.Option(
        None, "--api-key", help="default: ERP_PLANNER_API_KEY, or the provider's own env var"
    ),
    mode: ExecutionMode = typer.Option(ExecutionMode.SEQUENTIAL, "--mode", help="sequential | parallel"),
    concurrency: int = typer.Option(6, "--concurrency", help="workers, parallel mode only"),
    threshold: float = typer.Option(DEFAULT_THRESHOLD, "--threshold", help="agent-path cutoff"),
    no_agent: bool = typer.Option(False, "--no-agent", help="base path for everything"),
    escalate_below: float = typer.Option(
        0.75, "--escalate-below", help="send a cheap answer this unsure to the agent"
    ),
    max_iterations: int = typer.Option(6, "--max-iterations", help="tool rounds per agent run"),
    attempts: int = typer.Option(
        3, "--attempts", help="tries per cluster before the run gives up on it"
    ),
    sweeps: int = typer.Option(
        3, "--sweeps", help="passes back over the clusters that failed, at the end"
    ),
    db_url: str = typer.Option(
        None, "--db-url", envvar="ERP_PLANNER_DB_URL", help="live database for the agent's tools"
    ),
    reconcile: bool = typer.Option(
        None, "--reconcile/--no-reconcile", help="default: on for parallel, off for sequential"
    ),
    max_tables: int = typer.Option(12, "--max-tables"),
    max_columns: int = typer.Option(150, "--max-columns"),
    limit: int = typer.Option(None, "--limit", help="map only the first N clusters (a cheap trial)"),
) -> None:
    """Map a schema to an ontology. Costs money — run `map plan` first."""
    snapshot = _read(SchemaSnapshot, schema)
    clusters = cluster_schema(snapshot, max_tables=max_tables, max_columns=max_columns)
    if limit:
        clusters = clusters[:limit]

    key = api_key or api_key_for(provider)
    if not key:
        console.print(
            f"[red]no API key for {provider.value}[/red] — pass --api-key, or set one of "
            + ", ".join(API_KEY_ENV[provider])
            + " (a .env file is read automatically)."
        )
        raise typer.Exit(2)

    config = OrchestratorConfig(
        provider=provider,
        model=model,
        hard_model=hard_model,
        api_key=key,
        mode=mode,
        concurrency=concurrency,
        threshold=threshold,
        use_agent=not no_agent,
        escalate_below=escalate_below,
        max_iterations=max_iterations,
        attempts=attempts,
        sweeps=sweeps,
        db_url=db_url,
        reconcile=reconcile,
    )
    orchestrator = Orchestrator(config)
    bulk, hard = config.resolved_models()
    total = len(clusters)
    done = {"n": 0}

    console.print(
        f"[cyan]{mode.value}[/cyan]"
        + (f" x{concurrency}" if mode is ExecutionMode.PARALLEL else "")
        + f" · {provider.value} · cheap={bulk} agent={hard}"
        + ("" if not no_agent else " · [dim]agent path disabled[/dim]")
    )
    if not db_url and not no_agent:
        console.print("[dim]no --db-url: agent tools fall back to snapshot evidence only[/dim]")

    def progress(result, usage) -> None:
        done["n"] += 1
        # A sweep re-runs clusters already counted, so it numbers itself rather than running
        # past the total.
        position = f"{done['n']:>3}/{total}" if done["n"] <= total else "  retry"
        tag = (
            "[red]FAILED[/red]"
            if result.error
            else ("[yellow]agent[/yellow]" if result.path is MapPath.AGENT else "base")
        )
        confidence = result.min_confidence
        console.print(
            f"[dim]{position}[/dim] {tag:<18} "
            f"[dim]#{result.cluster.index:<3}[/dim] h={result.hardness.score:.2f} "
            f"{len(result.cluster.tables):>2}t "
            + (f"{result.tool_calls}tool " if result.tool_calls else "      ")
            + (f"c={confidence:.2f} " if confidence is not None else "       ")
            + f"{result.seconds:>5.1f}s ${usage.cost_usd:>6.3f}  "
            + (result.error or result.hardness.explain())
        )

    run = orchestrator.run(snapshot, clusters, on_result=progress)
    _write(run.ontology, out)
    # What the agent actually looked at, beside the answer it gave.
    if run.tool_log and len(run.tool_log):
        _write(run.tool_log, out.with_suffix(".tools.json"))

    o = run.ontology
    summary = RichTable(title="Mapping run", header_style="bold")
    summary.add_column("measure")
    summary.add_column("value", justify="right")
    for label, value in [
        ("clusters", str(total)),
        ("  base path", str(total - len(run.agent_results))),
        ("  agent path", str(len(run.agent_results))),
        ("  escalated", str(sum(1 for r in run.results if r.escalated))),
        ("  failed", f"[red]{len(run.failures)}[/red]" if run.failures else "0"),
        ("tool calls", str(sum(r.tool_calls for r in run.results))),
        ("clusters retried", str(run.ontology.run_metadata.get("cluster_retries", 0))),
        ("recovered by a later sweep", str(run.ontology.run_metadata.get("recovered_by_sweeps", 0))),
        ("rate-limit waits", str(run.ontology.run_metadata.get("rate_limit_waits", 0))),
        ("unparseable answers re-asked", str(run.ontology.run_metadata.get("parse_retries", 0))),
        ("classes / properties / relations", f"{len(o.classes)} / {len(o.properties)} / {len(o.relations)}"),
    ]:
        summary.add_row(label, value)
    if run.reconciliation:
        r = run.reconciliation
        rate = r.duplication_rate
        colour = "green" if rate <= 0.05 else "red"
        summary.add_row("concepts", f"{r.final} (from {r.proposed})")
        summary.add_row("  duplication rate", f"[{colour}]{rate:.1%}[/{colour}] (ceiling 5%)")
    summary.add_row("input tokens", f"{run.usage.input_tokens:,}")
    summary.add_row("  served from cache", f"{run.usage.cache_read_tokens:,}")
    summary.add_row("output tokens", f"{run.usage.output_tokens:,}")
    summary.add_row("wall clock", f"{run.seconds:.0f}s")
    priced = cost_is_known(provider, bulk, hard)
    summary.add_row(
        "cost",
        f"[bold]${run.usage.cost_usd:.2f}[/bold]"
        if priced
        else "[yellow]unknown[/yellow] (no price table for this model)",
    )
    console.print(summary)
    abandoned = run.ontology.run_metadata.get("abandoned_clusters", 0)
    if abandoned:
        # Silence here would read as "the schema was smaller than you thought".
        last = run.failures[-1].error if run.failures else ""
        console.print(
            f"\n[red]stopped after {len(run.failures)} clusters failed and none succeeded[/red] — "
            f"{abandoned} were not attempted."
            f"\n  [dim]last error: {last}[/dim]"
            "\n  [dim]a run that fails every cluster is not going to start working: check the key,"
            " the quota, or try --model with a less contended one, and lower --concurrency.[/dim]"
        )
    console.print(f"[green]wrote[/green] {out}")
