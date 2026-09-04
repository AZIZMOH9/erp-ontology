"""Shared plumbing for the command modules: one console, JSON in and out, and the model.

Anything two command modules both need lives here. They may not import each other -- a test
enforces it -- so that a phase stays independent of the phases around it."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

console = Console()


def _read(model, path: Path):
    """Load a pydantic model from a JSON file."""
    return model.model_validate_json(Path(path).read_text())


def _write(obj, path: Path) -> None:
    """Write a pydantic model as JSON, creating the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj.model_dump_json(indent=2) + "\n")


def _runner(provider, model: str | None, api_key: str | None):
    from erp_planner.llm.providers import api_key_for, default_models
    from erp_planner.llm.runner import ModelRunner

    key = api_key or api_key_for(provider)
    if not key:
        console.print(f"[red]no API key for {provider.value}[/red] — see --api-key or .env")
        raise typer.Exit(2)
    return ModelRunner(provider, model or default_models(provider)[0], key)
