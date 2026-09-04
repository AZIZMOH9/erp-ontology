# Data handling

*What leaves your network, and what does not.*

**The software runs inside your infrastructure.** It is a container you run, connecting to your
ERP over your own network. There is no hosted service holding your data, and no telemetry.

**Your database is read-only to us.** Ingestion issues `SELECT` statements and reads Postgres
catalogue tables. Nothing is written to your ERP at any point in the mapping pipeline.

**What is sent to the language model.** Only a schema description: table and column names, data
types, keys, row-count estimates, and a small number of sample values per column — 5 by default,
from the first 200 rows of each table. Primary and foreign key values are never sent.

**Sample values are masked by default.** Each value is replaced by its shape: `ACME GmbH` becomes
`AAAA AaaA`, `DE811907980` becomes `AA999999999`. Three modes are available per run, and every run
prints exactly what was masked and what was not before anything is sent:

| Mode | Behaviour |
|---|---|
| `all` (default) | Every sample value is masked. |
| `sensitive` | Columns detected as personal, financial or free-text are masked; business codes stay readable, which improves accuracy. |
| `none` | Raw sample values are sent. For customers who have decided their network boundary is sufficient. |

**Which model, and where, is your choice.** The LLM endpoint is configured by you and is the only
outbound connection the pipeline makes.

**Retention.** Snapshots and generated ontologies are files on your disk. Deleting them deletes
the data.
