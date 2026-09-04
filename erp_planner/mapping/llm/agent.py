"""The agent path: a worker that investigates before it answers.

Only clusters past the hardness threshold get here. Everything below it takes one fixed call with
no tools, which is what keeps the run cheap and the comparison honest.

Two phases, deliberately separate:

1. **Investigate** — the model calls tools until it stops asking or hits the iteration cap.
2. **Answer** — the same conversation, closed with a validated :class:`ClusterProposal`.

Mixing structured output into a tool loop behaves differently on every provider; "investigate,
then answer" behaves the same everywhere and reads back cleanly afterwards.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from erp_planner.clustering import Cluster
from erp_planner.llm.prompts import AGENT_ANSWER, AGENT_BRIEF
from erp_planner.llm.runner import ModelRunner
from erp_planner.mapping.llm.proposals import ClusterProposal
from erp_planner.mapping.llm.rendering import build_user_message
from erp_planner.mapping.llm.tools import Operation, SchemaTools


class SampleArgs(BaseModel):
    table: str = Field(description="Exact table name.")
    column: str = Field(description="Exact column name.")
    limit: int = Field(default=20, description="How many distinct values, max 40.")


class WalkArgs(BaseModel):
    table: str = Field(description="Exact table name.")


class SearchArgs(BaseModel):
    query: str = Field(description="Concept or table name fragment to look for.")


class StatsArgs(BaseModel):
    table: str = Field(description="Exact table name.")
    column: str = Field(description="Exact column name.")
    operation: Operation = Field(description="Which aggregate to run.")


def build_tools(schema_tools: SchemaTools) -> list[StructuredTool]:
    """Expose the tool implementations to the model, with typed arguments."""
    return [
        StructuredTool.from_function(
            func=schema_tools.fetch_sample_values,
            name="fetch_sample_values",
            description="Get more distinct values from one column. Values are masked.",
            args_schema=SampleArgs,
        ),
        StructuredTool.from_function(
            func=schema_tools.walk_foreign_keys,
            name="walk_foreign_keys",
            description="Show what a table references and what references it, beyond its cluster.",
            args_schema=WalkArgs,
        ),
        StructuredTool.from_function(
            func=schema_tools.search_concepts,
            name="search_concepts",
            description="Search concepts already established elsewhere in this run.",
            args_schema=SearchArgs,
        ),
        StructuredTool.from_function(
            func=schema_tools.column_statistics,
            name="column_statistics",
            description=(
                "Run one read-only aggregate over a column: distinct_count, value_frequency, "
                "min_max or null_fraction."
            ),
            args_schema=StatsArgs,
        ),
    ]


class AgentOutcome(BaseModel):
    proposal: ClusterProposal
    tool_calls: int = 0
    iterations: int = 0


def run_agent(
    runner: ModelRunner,
    cluster: Cluster,
    concepts: dict[str, str],
    schema_tools: SchemaTools,
    prefix: str,
    max_iterations: int = 6,
) -> AgentOutcome:
    """Investigate one cluster, then map it."""
    schema_tools.set_concepts(concepts)
    tools = build_tools(schema_tools)
    lookup = {t.name: t for t in tools}
    before = len(schema_tools.log)

    def execute(name: str, args: dict) -> str:
        tool = lookup.get(name)
        if tool is None:
            return f"No tool named {name!r}."
        return tool.func(**args)

    user = build_user_message(cluster, concepts) + "\n\n" + AGENT_BRIEF
    messages = runner.tool_loop(prefix, user, tools, execute, max_iterations=max_iterations)
    proposal = runner.structured_after(messages, AGENT_ANSWER, ClusterProposal)
    return AgentOutcome(
        proposal=proposal,
        tool_calls=len(schema_tools.log) - before,
        iterations=sum(1 for m in messages if m.type == "ai"),
    )
