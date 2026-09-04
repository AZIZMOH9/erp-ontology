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

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import Engine, create_engine

from erp_planner.clustering import Cluster, cluster_schema
from erp_planner.llm.prompts import RECONCILE_SYSTEM
from erp_planner.llm.providers import Provider, default_models
from erp_planner.llm.runner import ModelRunner, Usage, retry_delay
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
    # How many times a cluster is attempted before the run gives up on it. A cluster that fails
    # is lost from the ontology entirely, so it is worth several seconds to try again: the
    # failures seen in practice -- an answer that would not parse, a shared free-tier pool
    # returning 429 after its own retries -- are transient, and the same cluster succeeds on the
    # next attempt.
    attempts: int = 3
    # Give up on the whole run once this many clusters have failed without a single success.
    # Retrying is right for a cluster that failed by accident and wrong for a run that cannot
    # work at all: an exhausted quota or a saturated shared pool fails every cluster the same
    # way, and grinding through the rest costs two hours to produce nothing. 0 disables it.
    abandon_after: int = 5
    # After the main pass, the clusters that failed are tried again, as many times as this. The
    # cause is usually congestion, and by the end of a run the minutes that have passed are worth
    # more than any immediate retry: the pool that was saturated at cluster 3 is often free by
    # cluster 77. A sweep that recovers nothing ends it -- that is the difference between a
    # condition that clears and one that will not.
    sweeps: int = 3
    # How long to wait before a sweep, giving whatever failed time to clear.
    sweep_pause: float = 20.0

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
        # Mapping runs on several threads in parallel mode, so this counter needs a lock.
        self._lock = threading.Lock()
        self.cluster_retries = 0
        self.abandoned = 0
        self.sweeps_run = 0
        self.recovered = 0
        self._sweeping = False

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

    def _attempt(self, call):
        """Run a cluster, trying again before moving on rather than losing it to one bad call.

        A cluster that fails is absent from the ontology for the whole run -- four tables that
        nothing else will map. The failures seen in practice are transient: an answer that would
        not parse, or a shared free-tier pool answering 429 after the runner's own retries. The
        same cluster then succeeds on the next attempt, so it is worth a few seconds to ask again
        before moving on.

        Returns (outcome, None), or (None, what kept happening).
        """
        seen: list[str] = []
        for attempt in range(max(1, self.config.attempts)):
            try:
                outcome = call()
                if attempt:
                    with self._lock:
                        self.cluster_retries += attempt
                return outcome, None
            except Exception as exc:
                seen.append(f"{type(exc).__name__}: {exc}")
                if attempt + 1 < self.config.attempts:
                    # Honour a Retry-After when the provider sent one, else back off.
                    time.sleep(retry_delay(exc, attempt, cap=30.0))
        with self._lock:
            self.cluster_retries += len(seen) - 1
        # Naming the count matters: three failures for three different reasons is not one failure.
        return None, seen[-1] if len(seen) == 1 else f"failed {len(seen)}x, last: {seen[-1]}"

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

        outcome, failure = self._attempt(
            lambda: run_base_llm(self.bulk, cluster, concepts, prefix)
        )
        if outcome is None:
            return ClusterResult(
                cluster=cluster,
                path=Path.BASE,
                hardness=hardness,
                model=self.bulk.model_name,
                seconds=time.monotonic() - started,
                error=failure,
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
        outcome, failure = self._attempt(
            lambda: run_agent(
                self.hard,
                cluster,
                concepts,
                self._tools(snapshot),
                prefix,
                max_iterations=self.config.max_iterations,
            )
        )
        if outcome is None:
            return ClusterResult(
                cluster=cluster,
                path=Path.AGENT,
                hardness=hardness,
                model=self.hard.model_name,
                escalated=escalated,
                seconds=time.monotonic() - started,
                error=failure,
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
    def _hopeless(self, results: list[ClusterResult]) -> bool:
        """Has this run failed every cluster it has tried so far?

        Never during a sweep: a sweep is made entirely of clusters that already failed once, so
        it looks hopeless by construction. Judging it by the same rule abandoned runs that were
        in the middle of recovering.
        """
        limit = self.config.abandon_after
        if self._sweeping or not limit or len(results) < limit:
            return False
        return all(r.error for r in results)

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
            if self._hopeless(results):
                self.abandoned = len(clusters) - len(results)
                break
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
                if self._hopeless(list(collected.values())):
                    # Cancel what has not started. Threads already in flight run to completion.
                    self.abandoned = sum(1 for f in futures if f.cancel())
                    break
        return [collected[c.index] for c in clusters if c.index in collected]

    def _sweep(self, results, dispatch, prefix, snapshot, on_result):
        """Go back over the clusters that failed, rather than leaving holes in the ontology.

        Whatever made a cluster fail is usually a passing condition, and by the end of the run
        several minutes have gone by. Sweeping recovers clusters that an immediate retry could
        not, because time is the thing that fixes congestion.

        A sweep that recovers nothing stops the sweeping: a cause that has not cleared in a whole
        pass plus a pause is not going to clear on the next one.
        """
        if self.abandoned:
            return results  # the run was already judged hopeless; sweeping would grind
        for _ in range(max(0, self.config.sweeps)):
            failed = [r for r in results if r.error]
            if not failed:
                break
            if self.config.sweep_pause:
                time.sleep(self.config.sweep_pause)
            self.sweeps_run += 1
            self._sweeping = True
            again = {
                r.cluster.index: r
                for r in dispatch([r.cluster for r in failed], prefix, snapshot, on_result)
            }
            self._sweeping = False
            recovered = 0
            for i, result in enumerate(results):
                fresh = again.get(result.cluster.index)
                if result.error and fresh is not None and not fresh.error:
                    results[i] = fresh
                    recovered += 1
                elif result.error and fresh is not None:
                    results[i] = fresh  # keep the newest error, so the report is not stale
            self.recovered += recovered
            if not recovered:
                break
        return results

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
        results = self._sweep(results, dispatch, prefix, snapshot, on_result)

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
            "cluster_retries": self.cluster_retries,
            "abandoned_clusters": self.abandoned,
            "sweeps": self.sweeps_run,
            "recovered_by_sweeps": self.recovered,
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
