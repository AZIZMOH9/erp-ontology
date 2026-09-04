"""Everything in the mapping phase that exists to serve a model call.

    base_llm    the base system: one call per cluster, no tools
    agent       the agentic system: tools and an investigation loop
    reconcile   merging duplicate concepts after a parallel run
    proposals   the structured shape a model must return, and the fold into an Ontology
    rendering   turning a schema fragment into the text a model reads
    tools       what the agent may ask the database and the ontology

Separated from the phase's deterministic half -- the orchestrator that routes, and the hardness
function that decides -- so that changing a prompt or a tool cannot quietly change the routing.
"""

from erp_planner.mapping.llm.agent import AgentOutcome, build_tools, run_agent
from erp_planner.mapping.llm.base_llm import BaseLLMOutcome, run_base_llm
from erp_planner.mapping.llm.proposals import ClusterProposal, to_ontology
from erp_planner.mapping.llm.reconcile import ReconcileReport, deterministic_renames, reconcile
from erp_planner.mapping.llm.rendering import build_prefix, build_user_message
from erp_planner.mapping.llm.tools import Operation, SchemaTools, ToolLog

__all__ = [
    "AgentOutcome",
    "BaseLLMOutcome",
    "ClusterProposal",
    "Operation",
    "ReconcileReport",
    "SchemaTools",
    "ToolLog",
    "build_prefix",
    "build_tools",
    "build_user_message",
    "deterministic_renames",
    "reconcile",
    "run_agent",
    "run_base_llm",
    "to_ontology",
]
