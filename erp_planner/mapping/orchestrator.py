"""The orchestrator: score, route, dispatch, collect.

The rule that defines it: **the routing decision is code, never a model judgement.** That single
constraint is what keeps the run cheap, reproducible and measurable. The moment an LLM decides
which path a cluster takes, every run costs more, no two runs agree, and the question this design
exists to answer -- what did agency actually add? -- stops being answerable.

Per cluster:

1. Score hardness with a pure function (:mod:`~erp_planner.mapping.hardness`). No model call.
2. Route on a threshold. Config, not judgement.
3. **Base path** -- one structured call, cached prefix plus the cluster. No tools, no sub-agent.
4. **Agent path** -- a worker with tools that investigates before answering. Agentic machinery
   lives here and nowhere else.
5. Record path, hardness, confidence and tool calls, so base-versus-agent stays a fair comparison.

The orchestrator dispatches reconciliation; it does not perform it.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import Engine, create_engine

from erp_planner.clustering import Cluster, cluster_schema
from erp_planner.llm.prompts import RECONCILE_SYSTEM
from erp_planner.llm.providers import Provider, default_models
from erp_planner.llm.runner import ModelRunner, Usage
from erp_planner.mapping.hardness import DEFAULT_THRESHOLD, DEFAULT_WEIGHTS, Hardness, HardnessWeights
from erp_planner.mapping.hardness import score as score_hardness
from erp_planner.mapping.llm.agent import run_agent
from erp_planner.mapping.llm.base_llm import run_base_llm
from erp_planner.mapping.llm.proposals import ClusterProposal, to_ontology
from erp_planner.mapping.llm.reconcile import ReconcileReport, Reconciliation, reconcile
from erp_planner.mapping.llm.rendering import build_prefix
from erp_planner.mapping.llm.tools import SchemaTools, ToolLog
from erp_planner.masking import MaskingMode
from erp_planner.models import Ontology, SchemaSnapshot


class ExecutionMode(StrEnum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class Path(StrEnum):
    BASE = "base"
    AGENT = "agent"


@dataclass
class OrchestratorConfig:
    provider: Provider = Provider.ANTHROPIC
    model: str | None = None  # None -> the provider's default bulk model
    hard_model: str | None = None
    api_key: str | None = None
    max_tokens: int = 16000

    mode: ExecutionMode = ExecutionMode.SEQUENTIAL
    concurrency: int = 6

    # Routing. Both are configuration; tuning is expected.
    threshold: float = DEFAULT_THRESHOLD
    weights: HardnessWeights = DEFAULT_WEIGHTS
    use_agent: bool = True
    # A base-path answer this unsure is re-run on the agent path. Hardness is a prior; the
    # model's own confidence is evidence, and evidence may overrule a prior.
    escalate_below: float = 0.75
    max_iterations: int = 6

    # Tools. Without a database URL the agent still runs, on snapshot evidence alone.
    db_url: str | None = None
    schema: str = "public"
    masking: MaskingMode = MaskingMode.ALL

    reconcile: bool | None = None  # None -> on for parallel, off for sequential

    def resolved_models(self) -> tuple[str, str]:
        bulk, hard = default_models(self.provider)
        return self.model or bulk, self.hard_model or hard


@dataclass
class ClusterResult:
    cluster: Cluster
    path: Path
    hardness: Hardness
    model: str
    escalated: bool = False
    tool_calls: int = 0
    seconds: float = 0.0
    proposal: ClusterProposal | None = None
    error: str | None = None

    @property
    def min_confidence(self) -> float | None:
        if not self.proposal or not self.proposal.classes:
            return None
        return min(c.confidence for c in self.proposal.classes)


@dataclass
class MappingRun:
    ontology: Ontology
    usage: Usage
    results: list[ClusterResult] = field(default_factory=list)
    seconds: float = 0.0
    reconciliation: ReconcileReport | None = None
    tool_log: ToolLog | None = None

    @property
    def failures(self) -> list[ClusterResult]:
        return [r for r in self.results if r.error]

    @property
    def agent_results(self) -> list[ClusterResult]:
        return [r for r in self.results if r.path is Path.AGENT]


class Orchestrator:
    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config or OrchestratorConfig()
        bulk, hard = self.config.resolved_models()
        self.usage = Usage()
        self.bulk = ModelRunner(
            self.config.provider, bulk, self.config.api_key, self.config.max_tokens, self.usage
        )
        self.hard = (
            self.bulk
            if hard == bulk
            else ModelRunner(
                self.config.provider, hard, self.config.api_key, self.config.max_tokens, self.usage
            )
        )
        self._engine: Engine | None = None
        self.tool_log = ToolLog()

    # -- setup ---------------------------------------------------------------------------
    def _engine_for_tools(self) -> Engine | None:
        if self._engine is None and self.config.db_url:
            self._engine = create_engine(self.config.db_url)
        return self._engine

    def _tools(self, snapshot: SchemaSnapshot) -> SchemaTools:
        return SchemaTools(
            snapshot,
            engine=self._engine_for_tools(),
            masking=self.config.masking,
            schema=self.config.schema,
            log=self.tool_log,
        )

    # -- the two paths -------------------------------------------------------------------
    def map_cluster(
        self,
        cluster: Cluster,
        concepts: dict[str, str],
        prefix: str,
        snapshot: SchemaSnapshot,
    ) -> ClusterResult:
        hardness = score_hardness(cluster, self.config.weights)
        take_agent = self.config.use_agent and hardness.score >= self.config.threshold
        started = time.monotonic()

        if take_agent:
            return self._agent_path(cluster, concepts, prefix, snapshot, hardness, started)

        try:
            outcome = run_base_llm(self.bulk, cluster, concepts, prefix)
        except Exception as exc:
            return ClusterResult(
                cluster=cluster,
                path=Path.BASE,
                hardness=hardness,
                model=self.bulk.model_name,
                seconds=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )

        result = ClusterResult(
            cluster=cluster,
            path=Path.BASE,
            hardness=hardness,
            model=self.bulk.model_name,
            seconds=time.monotonic() - started,
            proposal=outcome.proposal,
        )
        weakest = result.min_confidence
        if (
            self.config.use_agent
            and weakest is not None
            and weakest < self.config.escalate_below
        ):
            escalated = self._agent_path(
                cluster, concepts, prefix, snapshot, hardness, started, escalated=True
            )
            # An escalation that fails leaves the base answer standing rather than nothing.
            return escalated if escalated.proposal else result
        return result

    def _agent_path(
        self,
        cluster: Cluster,
        concepts: dict[str, str],
        prefix: str,
        snapshot: SchemaSnapshot,
        hardness: Hardness,
        started: float,
        escalated: bool = False,
    ) -> ClusterResult:
        try:
            outcome = run_agent(
                self.hard,
                cluster,
                concepts,
                self._tools(snapshot),
                prefix,
                max_iterations=self.config.max_iterations,
            )
        except Exception as exc:
            return ClusterResult(
                cluster=cluster,
                path=Path.AGENT,
                hardness=hardness,
                model=self.hard.model_name,
                escalated=escalated,
                seconds=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
        return ClusterResult(
            cluster=cluster,
            path=Path.AGENT,
            hardness=hardness,
            model=self.hard.model_name,
            escalated=escalated,
            tool_calls=outcome.tool_calls,
            seconds=time.monotonic() - started,
            proposal=outcome.proposal,
        )

    # -- dispatch ------------------------------------------------------------------------
    def _sequential(self, clusters, prefix, snapshot, on_result) -> list[ClusterResult]:
        concepts: dict[str, str] = {}
        results = []
        for cluster in clusters:
            result = self.map_cluster(cluster, concepts, prefix, snapshot)
            results.append(result)
            if result.proposal:
                for proposed in result.proposal.classes:
                    concepts.setdefault(proposed.label, proposed.table)
            if on_result:
                on_result(result, self.usage)
        return results

    def _parallel(self, clusters, prefix, snapshot, on_result) -> list[ClusterResult]:
        collected: dict[int, ClusterResult] = {}
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
            futures = [
                pool.submit(self.map_cluster, c, {}, prefix, snapshot) for c in clusters
            ]
            for future in as_completed(futures):
                result = future.result()
                collected[result.cluster.index] = result
                if on_result:
                    on_result(result, self.usage)
        return [collected[c.index] for c in clusters]

    def _reconcile(self, ontology: Ontology, use_model: bool) -> ReconcileReport:
        def call_model(listing: str) -> Reconciliation:
            return self.hard.structured(RECONCILE_SYSTEM, listing, Reconciliation)

        try:
            return reconcile(ontology, call_model if use_model else None)
        except Exception:
            return reconcile(ontology, None)  # never lose a mapped ontology to a failed merge

    # -- entry point ---------------------------------------------------------------------
    def run(
        self,
        snapshot: SchemaSnapshot,
        clusters: list[Cluster] | None = None,
        on_result=None,
    ) -> MappingRun:
        clusters = clusters if clusters is not None else cluster_schema(snapshot)
        prefix = build_prefix(snapshot)
        started = time.monotonic()

        dispatch = (
            self._parallel if self.config.mode is ExecutionMode.PARALLEL else self._sequential
        )
        results = dispatch(clusters, prefix, snapshot, on_result)

        ontology = to_ontology(
            [r.proposal for r in results if r.proposal], schema_source=snapshot.source
        )
        should_reconcile = (
            self.config.reconcile
            if self.config.reconcile is not None
            else self.config.mode is ExecutionMode.PARALLEL
        )
        report = self._reconcile(ontology, use_model=should_reconcile)

        elapsed = time.monotonic() - started
        agent_results = [r for r in results if r.path is Path.AGENT]
        bulk, hard = self.config.resolved_models()
        ontology.obfuscation = snapshot.obfuscation
        ontology.run_metadata = {
            "provider": self.config.provider.value,
            "model": bulk,
            "hard_model": hard,
            "mode": self.config.mode.value,
            "concurrency": (
                self.config.concurrency if self.config.mode is ExecutionMode.PARALLEL else 1
            ),
            "hardness_threshold": self.config.threshold,
            "clusters": len(clusters),
            "agent_path": len(agent_results),
            "escalated": sum(1 for r in results if r.escalated),
            "tool_calls": sum(r.tool_calls for r in results),
            "rate_limit_waits": self.bulk.rate_limit_waits + (
                self.hard.rate_limit_waits if self.hard is not self.bulk else 0
            ),
            "parse_retries": self.bulk.parse_retries_used + (
                self.hard.parse_retries_used if self.hard is not self.bulk else 0
            ),
            "failed_clusters": sum(1 for r in results if r.error),
            "concepts_proposed": report.proposed,
            "concepts_final": report.final,
            "concept_duplication_rate": round(report.duplication_rate, 4),
            "prefix_tokens_estimate": len(prefix) // 4,
            "calls": self.usage.calls,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "cost_usd": round(self.usage.cost_usd, 4),
            "seconds": round(elapsed, 1),
        }
        return MappingRun(
            ontology=ontology,
            usage=self.usage,
            results=results,
            seconds=elapsed,
            reconciliation=report,
            tool_log=self.tool_log,
        )
