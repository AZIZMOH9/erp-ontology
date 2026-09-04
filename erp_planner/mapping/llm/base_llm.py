"""The base system: one call per cluster, no tools.

The bulk of any run goes through here -- 75 of 77 clusters on the demo Odoo. It gets the cached
prefix and the cluster, and returns a mapping. Nothing else: no tools, no loop, no second
opinion of its own.

That restraint is the point. Measured against the agentic system on schemas whose names carry
meaning, the two scored identically -- so paying for investigation there buys nothing. The agent
path earns its cost only where names are useless, which is exactly what the router decides.

It is a module rather than a line inside the orchestrator so that the two systems can be read,
tested and changed side by side; the orchestrator's job is to choose between them, not to be one
of them.
"""

from __future__ import annotations

from pydantic import BaseModel

from erp_planner.clustering import Cluster
from erp_planner.llm.runner import ModelRunner
from erp_planner.mapping.llm.proposals import ClusterProposal
from erp_planner.mapping.llm.rendering import build_user_message


class BaseLLMOutcome(BaseModel):
    """Deliberately the same shape the agentic system returns, so the caller need not care which
    one produced an answer."""

    proposal: ClusterProposal
    tool_calls: int = 0  # always zero; present so the two outcomes are interchangeable
    iterations: int = 1


def run_base_llm(
    runner: ModelRunner,
    cluster: Cluster,
    concepts: dict[str, str],
    prefix: str,
) -> BaseLLMOutcome:
    """Map one cluster in a single call.

    ``concepts`` is what earlier clusters established, and is empty in parallel mode where no
    cluster can see another's answer.
    """
    proposal = runner.structured(prefix, build_user_message(cluster, concepts), ClusterProposal)
    return BaseLLMOutcome(proposal=proposal)
