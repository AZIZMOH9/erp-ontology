# Workflow

![ERP to ontology pipeline](workflow.svg)

## At a glance

| # | Step | In | Out |
|---|---|---|---|
| 1 | Ingest | Odoo Postgres + XML-RPC, 511 tables | `schema.json` — 418 tables, 4,352 cols, 1,678 keys |
| 2 | Mask | raw sample values | shape masks — 209 of 1,188 columns |
| 3 | Cluster | 418 tables, 1,678 keys | 77 clusters, median 3 tables |
| 4 | Map | one cluster as text | `ontology.json` + confidence + cost |
| | | routing by `hardness()` — code, not a model | base path: 1 call, no tools |
| | | hardness >= 0.40 | agent path: tools, investigation |
| 5 | Verify | ontology | flagged queue *(not built)* |
| 6 | Review | flagged queue | accepted ontology + corrections *(not built)* |
| 7 | Serve | accepted ontology | queryable layer *(not built)* |
| 8 | Act | natural-language intent | sanctioned ERP API calls *(not built)* |

---

## Inside step 4 — the harness

![The agentic workflow](agentic-workflow.svg)

| Stage | In | Out |
|---|---|---|
| `hardness()` | one cluster | a score 0–1 and the table that drove it — **code, no model call** |
| route | score vs threshold 0.40 | base path (75 of 77) or agent path (2 of 77) |
| base path | cached prefix + cluster | one `ClusterProposal` + confidence, no tools |
| escalate | cheap answer below 0.75 confidence | the same cluster, re-run on the agent path |
| agent · investigate | cluster + 4 tools | tool results, until it stops asking (max 6 rounds) |
| agent · answer | that conversation | one validated `ClusterProposal` |
| collect | every result | tagged with path, hardness, confidence, tool calls |
| dispatch | all clusters | sequential (registry fed forward) or parallel (N workers) |
| reconcile | proposed concept names | merged concepts + duplication rate |

Full detail in [agentic-flow.md](agentic-flow.md).

## Commands

```bash
erp-planner ingest odoo --db-url ... --masking sensitive -o schema.json   # 1–2
erp-planner map plan schema.json                                          # 3
erp-planner map run  schema.json -o ontology.json                         # 4 sequential
erp-planner map run  schema.json --mode parallel --concurrency 6 -o o.json #   parallel
python -m benchmarks obfuscate schema.json --out split/ --level full     # benchmark
python -m benchmarks score prediction.json gold.json --aliases split/aliases.json
```
