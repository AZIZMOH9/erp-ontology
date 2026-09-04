"""Phase 2 -- automated semantic mapping.

The phase has two halves, deliberately kept apart:

    orchestrator.py   routes each cluster and dispatches, calling no model itself
    hardness.py       the deterministic router: same cluster, same score, every run
    llm/              everything that exists to serve a model call

Clustering and naming used to live here and no longer do. Neither is mapping: clustering turns a
schema into work units before any of this begins, and the naming convention is applied to what
comes back and is also used by the scorer.
"""

from erp_planner.mapping.hardness import (
    DEFAULT_THRESHOLD,
    DEFAULT_WEIGHTS,
    Hardness,
    HardnessWeights,
    score,
    table_hardness,
    takes_agent_path,
)
from erp_planner.mapping.llm import (
    ClusterProposal,
    Operation,
    ReconcileReport,
    SchemaTools,
    ToolLog,
    build_prefix,
    build_tools,
    build_user_message,
    deterministic_renames,
    reconcile,
    run_agent,
    run_base_llm,
    to_ontology,
)
from erp_planner.mapping.orchestrator import (
    ClusterResult,
    ExecutionMode,
    MappingRun,
    Orchestrator,
    OrchestratorConfig,
    Path,
)

__all__ = [
    "DEFAULT_THRESHOLD",
    "DEFAULT_WEIGHTS",
    "ClusterProposal",
    "ClusterResult",
    "ExecutionMode",
    "Hardness",
    "HardnessWeights",
    "MappingRun",
    "Operation",
    "Orchestrator",
    "OrchestratorConfig",
    "Path",
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
    "score",
    "table_hardness",
    "takes_agent_path",
    "to_ontology",
]
