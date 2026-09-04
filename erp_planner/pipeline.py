"""A guided run through the whole pipeline, one step at a time.

Each step says what it does, what it reads, what it writes and what it costs, then waits. Nothing
runs until it is accepted. The steps that spend money are marked, and the ones that already have
output offer to reuse it rather than paying twice.

The point is not convenience -- `mise run map` is already one word. The point is that the pipeline
has steps with real costs and real prerequisites, and a first-time user should see them before
they happen rather than after.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

# Anything that would print a credential to the terminal or into a task log.
_SECRET_FLAG = re.compile(r"(--(?:odoo-)?(?:password|api-key)[= ])(\S+)")
_SECRET_URL = re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)")


def redact(text: str) -> str:
    """Hide passwords in a command before it is shown.

    The demo credentials are public, but the same panel prints a customer's real ones, and a
    terminal is copied into tickets and screenshots.
    """
    text = _SECRET_FLAG.sub(r"\1********", text)
    return _SECRET_URL.sub(r"\1********\3", text)


# Where to get a key, per provider. A user who has never used one should not have to search.
KEY_SIGNUP = {
    "google": "https://aistudio.google.com/apikey",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
}


def how_to_supply(console: Console, name: str, env_var: str, example: str, flag: str) -> None:
    """Explain every way to provide a missing setting, not just the prompt in front of them.

    A prompt alone teaches nothing: the next run, or the next person, or CI, needs to know that
    the same value can come from a flag or the environment. Four routes, most convenient first.
    """
    console.print(
        f"\n  [bold]how to provide {name}[/bold] — any one of these:\n"
        f"    [dim]1.[/dim] answer the prompt below      [dim](offered for saving to {ENV_FILE})[/dim]\n"
        f"    [dim]2.[/dim] put it in {ENV_FILE}               [dim]{env_var}={example}[/dim]\n"
        f"    [dim]3.[/dim] export it                    [dim]export {env_var}={example}[/dim]\n"
        f"    [dim]4.[/dim] pass it per command          [dim]{flag}[/dim]"
    )


def database_name(db_url: str) -> str:
    """The database a connection string points at.

    Odoo's XML-RPC call needs the database name, and it is the same one the Postgres URL already
    names. It used to be hardcoded to the demo's `erp_planner`, so a user connecting to their own
    Odoo authenticated against a database that does not exist.
    """
    tail = db_url.rsplit("/", 1)[-1]
    return tail.split("?", 1)[0] or ""


def normalise_url(db_url: str) -> str:
    """Point a plain Postgres URL at the driver we actually ship.

    Everyone writes `postgresql://user:pass@host/db`. SQLAlchemy reads that as psycopg2, which is
    not installed, and reports a ModuleNotFoundError -- which reads as a broken tool, not as a
    URL that needs a suffix nobody has heard of.
    """
    for prefix in ("postgresql://", "postgres://"):
        if db_url.startswith(prefix):
            return "postgresql+psycopg://" + db_url[len(prefix):]
    return db_url


def reachable(db_url: str, timeout: int = 5) -> tuple[bool, str]:
    """Can we actually connect? Returned as a fact, not an exception."""
    db_url = normalise_url(db_url)
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(db_url, connect_args={"connect_timeout": timeout})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "connected"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"


@dataclass
class Step:
    key: str
    title: str
    what: str
    command: list[str]
    reads: list[Path] = field(default_factory=list)
    writes: list[Path] = field(default_factory=list)
    cost: str = "free"
    minutes: str = "seconds"
    note: str = ""

    @property
    def costs_money(self) -> bool:
        return self.cost != "free"

    def missing_inputs(self) -> list[Path]:
        return [p for p in self.reads if not p.exists()]

    def already_done(self) -> bool:
        return bool(self.writes) and all(p.exists() for p in self.writes)


DEMO_DB = "postgresql+psycopg://odoo:odoo@localhost:5433/erp_planner"
DEMO_ODOO_URL = "http://localhost:8069"
URL_TEMPLATE = "postgresql+psycopg://user:password@host:5432/database"
ENV_FILE = Path(".env")


def remember(values: dict[str, str], path: Path = ENV_FILE) -> None:
    """Save connection details to .env so the next run connects without asking.

    .env is gitignored and mise reads it with redaction. Writing a password to a file on disk is
    the user's call, so this is offered rather than done -- but retyping a connection string every
    run is the reason nobody uses a tool twice.
    """
    existing: dict[str, str] = {}
    order: list[str] = []
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value
                order.append(key.strip())
            else:
                order.append(line)
    existing.update({k: v for k, v in values.items() if v})

    out, written = [], set()
    for entry in order:
        if entry in existing and entry not in written:
            out.append(f"{entry}={existing[entry]}")
            written.add(entry)
        elif entry not in existing:
            out.append(entry)
    out.extend(f"{k}={v}" for k, v in existing.items() if k not in written)
    path.write_text("\n".join(out).rstrip() + "\n")


def connect(console: Console, db_url: str, odoo_url: str) -> tuple[str, str, str, str] | None:
    """Work out what to connect to, before anything reads a schema.

    Without this the first step is a wall of somebody else's connection string. A first-time user
    has three real situations -- the demo is already running, the demo is not started yet, or they
    have their own ERP -- and only the last one needs typing.

    Returns (db_url, odoo_url, user, password), or None if the user gave up.
    """
    db_url = normalise_url(db_url)
    ok, detail = reachable(db_url)
    if ok:
        console.print(f"[green]connected[/green] to {redact(db_url)}")
        # Only the bundled demo has admin/admin. Someone else's ERP does not, and defaulting to
        # it turns a missing credential into a failed login they have to diagnose.
        fallback = "admin" if db_url == DEMO_DB else ""
        return (
            db_url, odoo_url,
            os.environ.get("ERP_PLANNER_ODOO_USER", fallback),
            os.environ.get("ERP_PLANNER_ODOO_PASSWORD", fallback),
        )

    console.print(f"[yellow]cannot reach[/yellow] {redact(db_url)}\n  [dim]{detail}[/dim]")
    how_to_supply(
        console, "the database connection", "ODOO_DB", URL_TEMPLATE,
        "erp-planner ingest odoo --db-url postgresql://…",
    )
    console.print(
        "\n  [bold]1[/bold] start the local demo Odoo   [dim](docker, ~10 min the first time)[/dim]"
        "\n  [bold]2[/bold] connect to my own ERP       [dim](you will be asked for details)[/dim]"
        "\n  [bold]3[/bold] give up"
    )
    try:
        choice = Prompt.ask("  [bold]which[/bold]", choices=["1", "2", "3"], default="1")
    except (EOFError, KeyboardInterrupt):
        return None

    if choice == "3":
        return None
    if choice == "1":
        console.print("[dim]starting containers…[/dim]")
        subprocess.run(["docker", "compose", "-f", "docker/docker-compose.yml", "up", "-d"], check=False)
        ok, detail = reachable(DEMO_DB, timeout=15)
        if not ok:
            console.print(
                f"[red]still not reachable[/red] — {detail}\n"
                "[dim]the database exists only after `mise run odoo:seed`, which takes ~10 min[/dim]"
            )
            return None
        console.print("[green]the demo Odoo is up[/green]")
        return DEMO_DB, DEMO_ODOO_URL, "admin", "admin"

    console.print(
        "\n[dim]The database URL is a read-only connection. Odoo's HTTP URL is optional but "
        "worth giving: it supplies field labels and marks which tables are customisations.[/dim]"
    )
    console.print(f"  [dim]form: {URL_TEMPLATE}[/dim]")
    # No default: offering the demo URL here invites someone to press enter and connect to the
    # wrong database.
    url = normalise_url(Prompt.ask("  database URL").strip())
    ok, detail = reachable(url)
    if not ok:
        console.print(f"[red]cannot connect[/red] — {detail}")
        return None
    console.print("[green]connected[/green]")

    http = Prompt.ask("  Odoo URL (blank to skip)", default="")
    user = Prompt.ask("  Odoo user", default="admin") if http else ""
    password = Prompt.ask("  Odoo password", password=True, default="") if http else ""

    if Prompt.ask(
        f"  save these to {ENV_FILE} so the next run connects without asking?",
        choices=["y", "n"], default="y",
    ) == "y":
        remember({
            "ODOO_DB": url, "ODOO_URL": http,
            "ERP_PLANNER_ODOO_USER": user, "ERP_PLANNER_ODOO_PASSWORD": password,
        })
        console.print(f"  [green]saved[/green] to {ENV_FILE} [dim](gitignored)[/dim]")
    return url, http, user, password


def choose_provider(console: Console) -> str:
    """Which model provider to use. Asked once, then remembered.

    The pipeline previously hardcoded Google because that is what was to hand. The system is
    provider-neutral by design -- that is the whole point of the LangChain layer -- so the choice
    belongs to whoever runs it.
    """
    from erp_planner.llm.providers import DEFAULT_MODELS, Provider

    existing = os.environ.get("ERP_PLANNER_PROVIDER")
    if existing:
        try:
            Provider(existing)
            console.print(f"[green]provider:[/green] {existing}")
            return existing
        except ValueError:
            console.print(f"[yellow]ignoring unknown provider {existing!r}[/yellow]")

    console.print("\n  [bold]which model provider?[/bold]")
    options = [
        ("1", Provider.ANTHROPIC, "Claude", "console.anthropic.com"),
        ("2", Provider.GOOGLE, "Gemini", "aistudio.google.com"),
        ("3", Provider.OPENAI, "GPT", "platform.openai.com"),
    ]
    # Which provider defaults have actually been run. Gemini's were once gemini-2.5-*, which
    # turned out to be retired; saying which are guesses costs nothing and saves a confusing 404.
    verified = {Provider.GOOGLE: "verified", Provider.ANTHROPIC: "from a maintained table"}
    for number, provider, family, where in options:
        bulk, hard = DEFAULT_MODELS[provider]
        status = verified.get(provider, "[yellow]defaults unverified[/yellow]")
        console.print(
            f"    [bold]{number}[/bold] {family:<7} [dim]{bulk} / {hard}[/dim]  "
            f"({status})  [dim]key from {where}[/dim]"
        )
    console.print(
        "    [dim]only Anthropic caches the shared prompt prefix; the others pay full price "
        "for it on every call. --model overrides any of these.[/dim]"
    )
    try:
        pick = Prompt.ask("  [bold]which[/bold]", choices=["1", "2", "3"], default="2")
    except (EOFError, KeyboardInterrupt):
        return "google"

    chosen = dict((n, p) for n, p, _, _ in options)[pick].value
    if Prompt.ask(f"  remember {chosen} in {ENV_FILE}?", choices=["y", "n"], default="y") == "y":
        remember({"ERP_PLANNER_PROVIDER": chosen})
        console.print(f"  [green]saved[/green] to {ENV_FILE}")
    os.environ["ERP_PLANNER_PROVIDER"] = chosen
    return chosen


def ensure_api_key(console: Console, provider: str) -> bool:
    """Check for a model key before anything is done, not when the first paid step fails.

    Without this a new user connects, ingests and plans -- several minutes -- and only then hits
    "no API key". The check is free and the failure is certain, so it belongs at the front.
    """
    from erp_planner.llm.providers import API_KEY_ENV, Provider, api_key_for

    try:
        which = Provider(provider)
    except ValueError:
        console.print(f"[red]unknown provider {provider!r}[/red]")
        return False
    if api_key_for(which):
        console.print(f"[green]{provider} API key found[/green]")
        return True

    console.print(
        f"\n[yellow]no {provider} API key[/yellow] — the mapping steps need one.\n"
        f"  [dim]looked in: {', '.join(API_KEY_ENV[which])}[/dim]"
    )
    # The provider's own variable, not the generic override and not whichever is last.
    natural = API_KEY_ENV[which][1] if len(API_KEY_ENV[which]) > 1 else API_KEY_ENV[which][0]
    how_to_supply(
        console, f"a {provider} API key", natural, "…",
        f"erp-planner map run --provider {provider} --api-key …",
    )
    if provider in KEY_SIGNUP:
        console.print(f"    [dim]get one at {KEY_SIGNUP[provider]}[/dim]")
    try:
        key = Prompt.ask("  paste a key (blank to skip the paid steps)", password=True, default="")
    except (EOFError, KeyboardInterrupt):
        return False
    if not key.strip():
        console.print("[dim]continuing without one; the paid steps will be skipped[/dim]")
        return False

    os.environ[natural] = key.strip()
    if Prompt.ask(f"  save it to {ENV_FILE}?", choices=["y", "n"], default="y") == "y":
        remember({natural: key.strip()})
        console.print(f"  [green]saved[/green] to {ENV_FILE} [dim](gitignored)[/dim]")
    return True


def build_steps(
    runs: Path,
    db_url: str,
    odoo_url: str,
    provider: str,
    model: str,
    hard_model: str,
    user: str = "",
    password: str = "",
) -> list[Step]:
    schema, ontology = runs / "schema.json", runs / "ontology.json"
    second, queue = runs / "second.json", runs / "queue.json"
    return [
        Step(
            key="ingest", title="1 · Ingest",
            what="Read the ERP into a schema snapshot: tables, columns, keys, sample rows, and "
                 "Odoo's own field labels. Sample values are masked before anything leaves.",
            command=["erp-planner", "ingest", "odoo", "--db-url", db_url]
            + (["--odoo-url", odoo_url, "--odoo-db", database_name(db_url),
                "--odoo-user", user, "--odoo-password", password] if odoo_url else [])
            + ["--masking", "sensitive", "--out", str(schema)],
            writes=[schema], minutes="~5s",
            note="" if odoo_url else "No Odoo URL: structure only, without field labels.",
        ),
        Step(
            key="plan", title="2 · Plan (free)",
            what="Group tables into foreign-key neighbourhoods and score each one's hardness. "
                 "Shows exactly which clusters would go to the expensive agent path.",
            command=["erp-planner", "map", "plan", str(schema), "--show", "12"],
            reads=[schema], minutes="instant",
            note="Read this before accepting the next step - it is what decides the bill.",
        ),
        Step(
            key="map", title="3 · Map  ← COSTS MONEY",
            what="Infer what every table and column means. Cheap model for routine clusters, the "
                 "stronger one with database tools for the hard ones.",
            command=["erp-planner", "map", "run", str(schema), "--provider", provider,
                     "--model", model, "--hard-model", hard_model, "--mode", "parallel",
                     "--concurrency", "6", "--db-url", db_url, "--out", str(ontology)],
            reads=[schema], writes=[ontology], cost="~$0.40", minutes="~4 min",
        ),
        Step(
            key="second", title="4 · Second opinion  ← COSTS MONEY",
            what="Map the schema again, independently, with a different configuration. "
                 "Disagreement between two runs is the strongest error signal available.",
            command=["erp-planner", "map", "run", str(schema), "--provider", provider,
                     "--model", model, "--no-agent", "--mode", "parallel", "--concurrency", "6",
                     "--out", str(second)],
            reads=[schema], writes=[second], cost="~$0.30", minutes="~4 min",
            note="Skippable, but verification catches roughly half as much without it.",
        ),
        Step(
            key="verify", title="5 · Verify (free)",
            what="Score every mapping on evidence independent of the model, and produce a review "
                 "queue ordered worst-first. No API calls.",
            command=["erp-planner", "verify", "run", str(ontology), "--schema", str(schema),
                     "--against", str(second), "--out", str(queue), "--show", "20"],
            reads=[ontology], writes=[queue], minutes="instant",
        ),
        Step(
            key="judge", title="6 · Judge  ← COSTS A LITTLE",
            what="Most flagged mappings are two runs wording the same meaning differently, not "
                 "disagreeing about it. Judging them turns a queue nobody would work through "
                 "into one somebody might — measured 59% flagged down to roughly 2%.",
            command=["erp-planner", "verify", "judge", str(ontology), "--schema", str(schema),
                     "--against", str(second), "--queue", str(queue), "--provider", provider,
                     "--model", model, "--out", str(queue)],
            reads=[ontology, second, queue], writes=[queue],
            cost="~$0.20", minutes="~2 min",
            note="Skippable. Without it the review queue is mostly naming differences.",
        ),
        Step(
            key="export", title="7 · Export (free)",
            what="Write the ontology as OWL/Turtle, which opens in Protégé or webvowl.org.",
            command=["erp-planner", "export", str(ontology), "--out", str(runs / "ontology.ttl")],
            reads=[ontology], writes=[runs / "ontology.ttl"], minutes="instant",
        ),
        Step(
            key="graph", title="8 · Graph (free)",
            what="Render the ontology's backbone as an SVG and a PNG, coloured by confidence.",
            command=["erp-planner", "export", str(ontology), "--out", str(runs / "graph.dot"),
                     "--limit", "28"],
            reads=[ontology], writes=[runs / "graph.svg"], minutes="instant",
        ),
    ]


def _describe(step: Step, console: Console, index: int, total: int) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="dim", justify="right")
    body.add_column()
    body.add_row("does", step.what)
    if step.reads:
        body.add_row("reads", ", ".join(str(p) for p in step.reads))
    if step.writes:
        body.add_row("writes", ", ".join(str(p) for p in step.writes))
    body.add_row("cost", f"[red]{step.cost}[/red]" if step.costs_money else "free")
    body.add_row("takes", step.minutes)
    body.add_row("runs", redact(" ".join(shlex.quote(c) for c in step.command)))
    if step.note:
        body.add_row("note", f"[yellow]{step.note}[/yellow]")
    console.print(
        Panel(body, title=f"[bold]{step.title}[/bold]  [dim]({index}/{total})[/dim]",
              border_style="red" if step.costs_money else "blue")
    )


def run(
    steps: list[Step],
    console: Console | None = None,
    assume_yes: bool = False,
    known: list[Step] | None = None,
) -> int:
    """Walk the steps, asking before each. Returns the number that ran.

    ``known`` is the full pipeline, used only to name which step produces a missing input. Without
    it, a filtered run (`--only verify`) cannot tell you that `map` is what you are missing.
    """
    console = console or Console()
    known = known or steps
    ran = 0
    accept_rest = assume_yes

    for index, step in enumerate(steps, start=1):
        missing = step.missing_inputs()
        if missing:
            producers = {
                str(path): other.key for other in known for path in other.writes
            }
            needed = ", ".join(str(m) for m in missing)
            makers = sorted({producers[str(m)] for m in missing if str(m) in producers})
            console.print(
                f"[yellow]skipping {step.title}[/yellow] — it needs {needed}"
                + (
                    f"\n  [dim]run the {' and '.join(makers)} step first, "
                    f"or: erp-planner pipeline --only {' --only '.join(makers)}[/dim]"
                    if makers
                    else "\n  [dim]nothing in this pipeline produces that file[/dim]"
                )
            )
            continue

        _describe(step, console, index, len(steps))
        if step.already_done() and not accept_rest:
            console.print("[dim]output already exists; running again will overwrite it[/dim]")

        if not accept_rest:
            console.print("[dim]  y run · n skip · a run all remaining · q quit[/dim]")
            try:
                choice = Prompt.ask(
                    "  [bold]run this step?[/bold]",
                    choices=["y", "n", "a", "q"], default="y", show_choices=False,
                )
            except (EOFError, KeyboardInterrupt):
                # No terminal (a pipe, or CI). Refusing is the safe reading of no answer.
                console.print("\n[dim]no input available; stopping rather than assuming yes[/dim]")
                break
            if choice == "q":
                console.print("[dim]stopped.[/dim]")
                break
            if choice == "n":
                console.print(f"[dim]skipped {step.key}.[/dim]\n")
                continue
            if choice == "a":
                accept_rest = True

        started = time.monotonic()
        result = subprocess.run(step.command, check=False)
        elapsed = time.monotonic() - started
        if result.returncode != 0:
            console.print(f"[red]{step.key} failed[/red] (exit {result.returncode}) after {elapsed:.0f}s")
            if accept_rest:
                console.print("[dim]stopping: a later step would read output this one did not write[/dim]")
                break
            if Prompt.ask("  continue anyway?", choices=["y", "n"], default="n") == "n":
                break
        else:
            ran += 1
            console.print(f"[green]{step.key} done[/green] in {elapsed:.0f}s\n")

    return ran


# Where a run's output goes. Relative to wherever the user is standing, not to wherever this
# package happens to be installed -- their ontology is their data and does not belong inside our
# source tree, which is where it used to land.
# What each file a run leaves behind actually is. The pipeline named these one step at a time,
# which meant that by the end nobody could say where the ontology was without scrolling back.
ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("schema.json", "the schema as read: tables, columns, keys, masked sample values"),
    ("ontology.json", "the ontology: classes, their properties, and the relations between them"),
    ("second.json", "the independent second mapping, kept so verify can compare the two"),
    ("queue.json", "the review queue, least trustworthy first"),
    ("ontology.ttl", "the ontology as OWL — opens in Protege or webvowl.org"),
    ("graph.svg", "the ontology drawn — open in a browser"),
    ("graph.png", "the same picture as an image"),
    ("graph.dot", "Graphviz source for the picture"),
    ("ontology.tools.json", "what the agent asked the database while it was mapping"),
)


def report_outputs(console: Console, runs: Path) -> None:
    """Say where everything went, once, at the end."""
    from rich.table import Table

    present = [(name, what) for name, what in ARTIFACTS if (runs / name).exists()]
    if not present:
        console.print("\n[dim]no output files were written[/dim]")
        return

    table = Table(title=f"Written to {runs.resolve()}", title_justify="left", show_lines=False)
    table.add_column("file", style="bold")
    table.add_column("size", justify="right")
    table.add_column("what it is")
    for name, what in present:
        size = (runs / name).stat().st_size
        human = f"{size / 1_000_000:.1f}M" if size >= 1_000_000 else f"{size // 1000 or 1}K"
        table.add_row(str(runs / name), human, what)
    console.print()
    console.print(table)
    if (runs / "ontology.ttl").exists():
        console.print(f"  [dim]the ontology:[/dim] open {runs / 'ontology.ttl'} in Protege")
    if (runs / "graph.svg").exists():
        console.print(f"  [dim]the picture:[/dim]   open {runs / 'graph.svg'}")


DEFAULT_OUTPUT = "erp-planner"


def output_dir() -> Path:
    return Path(os.environ.get("ERP_PLANNER_HOME") or os.environ.get("RUNS") or DEFAULT_OUTPUT)


def default_steps(connection: tuple[str, str, str, str] | None = None) -> list[Step]:
    runs = output_dir()
    from erp_planner.llm.providers import Provider, default_models

    provider = os.environ.get("ERP_PLANNER_PROVIDER", "google")
    try:
        bulk_default, hard_default = default_models(Provider(provider))
    except ValueError:
        provider, (bulk_default, hard_default) = "google", default_models(Provider.GOOGLE)

    configured = os.environ.get("ODOO_DB", DEMO_DB)
    db_url, odoo_url, user, password = connection or (
        configured,
        os.environ.get("ODOO_URL", ""),
        os.environ.get("ERP_PLANNER_ODOO_USER", "admin" if configured == DEMO_DB else ""),
        os.environ.get("ERP_PLANNER_ODOO_PASSWORD", "admin" if configured == DEMO_DB else ""),
    )
    return build_steps(
        runs, db_url, odoo_url,
        provider,
        os.environ.get("ERP_PLANNER_MODEL", bulk_default),
        os.environ.get("ERP_PLANNER_HARD_MODEL", hard_default),
        user, password,
    )
