# Retail Insights: data-analysis chat agent for retail executives

A production-minded prototype of a conversational analyst for **OpsFleet**'s retail client. Executives
ask questions in plain language ("Why are users in Texas underspending vs California?", "Create a report
for Q1 with action items for Q2"), discuss the answers, and save reports. The agent writes and runs
BigQuery SQL over `bigquery-public-data.thelook_ecommerce`, reuses how human analysts approached similar
questions (the "golden" trios), and enforces privacy, per-user data scoping, safe deletion, cost limits
and graceful failure **in code, not just in the prompt**.

Built with **Google ADK + Gemini + BigQuery**, CLI interface, fully runnable offline too (DuckDB +
Ollama or a deterministic model).

| | |
|---|---|
| Architecture (HLD, Mermaid) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Technical design (choices, data flow, every requirement, error handling, sizing) | [docs/TECHNICAL_DESIGN.md](docs/TECHNICAL_DESIGN.md) |
| Evaluation / QA | [docs/EVALUATION.md](docs/EVALUATION.md) |
| Observability | [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) |
| Example run (real transcripts) | [docs/EXAMPLE_RUN.md](docs/EXAMPLE_RUN.md) |
| Assumptions and client answers | [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) |
| Requirement → design → code → test | [docs/REQUIREMENTS_TRACEABILITY.md](docs/REQUIREMENTS_TRACEABILITY.md) |

## What it does

* **Natural-language analysis** over orders, order items, products and customers: customer behaviour,
  product X vs Y and *why*, time series, YTD, database-structure questions, and multi-step "why" questions
  (the agent plans several queries and synthesises them).
* **Reports with action items**, saved to a private per-user library; list, read and delete them
  conversationally.
* **Hybrid intelligence**: retrieves the most relevant analyst trios (question → SQL → report) for every
  message and follows their metric definitions and interpretation.
* **Safety & PII** *(required in prototype)*: PII columns are physically unreachable (SQL rewritten onto
  PII-free views), demographics only in aggregates of at least 5 customers (k-anonymity injected by the
  SQL guard), value-level PII scanning of results and answers, pseudonymous customer ids, per-user
  product entitlements, prompt-injection and off-topic guard.
* **High-stakes oversight** *(required)*: the model can only *request* a deletion; the CLI shows the
  exact reports and the user must type `DELETE <n>`. The confirmation is token-bound (user, conversation,
  snapshot, 5-minute TTL, single use) and executed by deterministic code.
* **Resilience** *(required)*: dry-run + byte caps, self-correction with structured hints and
  empty-result diagnostics, at most 3 failed attempts, $/call budgets per question, retries + circuit
  breaker for BigQuery, Gemini retry + fallback model, degraded answers from the golden library if all
  models are down, and an error boundary so the CLI never crashes. Fault injection flags included.
* **Observability** *(required)*: per-turn traces of every model message, tool call, SQL attempt
  (original and rewritten), guard decision, tokens and cost; `trace` and `stats` commands.
* **Learning loop**: explicit and implicit per-user preferences (tables vs bullets, depth, charts);
  positively rated conversations become candidate trios that pass an automated promotion gate.
* **Persona agility**: tone and report structure in YAML, published through an automated quality gate,
  live on the next message without redeploy, with instant rollback.
* **Extensible**: tools are registered functions; `create_chart` ships as the example extension.

## Setup

### Prerequisites
* Python **3.10 – 3.13** (tested with 3.12) and git.
* For online mode: a Google Cloud project with the BigQuery API enabled (the free tier is plenty; each
  query here scans about 3–10 MB) and the [gcloud CLI](https://cloud.google.com/sdk/docs/install); a
  free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).
* Optional, offline LLM: [Ollama](https://ollama.com) with a tool-calling model (e.g. `qwen3.5:4b`).

### Install
```bash
git clone <this repository> retail-agent
cd retail-agent
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```
`requirements.txt` also installs the project itself (editable), which provides the `retail-agent`
command.

### Configure (online: Gemini + BigQuery)
```bash
cp .env.example .env          # Windows: copy .env.example .env
# edit .env: GOOGLE_API_KEY=<your AI Studio key>, RA_GCP_PROJECT=<your GCP project id>
gcloud auth application-default login
gcloud services enable bigquery.googleapis.com --project <your GCP project id>
retail-agent doctor           # checks BigQuery and both Gemini models
```
All settings are in `.env.example` (models, budgets, k, byte cap, ...). BigQuery auth follows the
standard [client-library ADC flow](https://cloud.google.com/bigquery/docs/reference/libraries); a
service account key via `GOOGLE_APPLICATION_CREDENTIALS` works too.

### Run
```bash
retail-agent chat                       # as the CEO (all products)
retail-agent chat --user head_of_womens # a category lead: sees only Womenswear
retail-agent users                      # who can see what
retail-agent ask "Compare Levi's and Diesel - why do they perform differently?" --trace
```
In chat: `/help`, `/reports`, `/prefs`, `/feedback + | - [comment]`, `/trace`, `/stats`, `/persona`,
`/whoami`, `/new`, `/quit`.

### Fully offline (no cloud, no keys)
```bash
python scripts/build_offline_db.py                         # synthetic thelook copy (DuckDB, ~5 s)
retail-agent chat --provider fake --warehouse duckdb       # deterministic offline model
retail-agent chat --provider ollama --warehouse duckdb     # local LLM (set RA_OLLAMA_MODEL, e.g. ollama_chat/qwen3.5:4b)
```

### Tests and evaluation
```bash
pytest                                   # ~100 offline tests (unit, adversarial, end-to-end), ~15 s
python evals/run_evals.py                # offline eval gate
python evals/run_evals.py --provider gemini --warehouse bigquery --delay 3   # online eval gate
python scripts/smoke_online.py           # live schema drift check + all golden trios on BigQuery + Gemini ping
```

### Admin commands
```bash
retail-agent persona publish config/personas/upbeat_coach.yaml   # gate -> live, no redeploy
retail-agent persona rollback | history | show
retail-agent learn [--revalidate]        # mine rated turns -> candidate trios -> automated gate
retail-agent stats [--hours 24]          # agent metrics
retail-agent trace [TRACE_ID]            # deep dive into a turn
```
Resilience demo: `retail-agent chat --chaos 0.5 --llm-chaos 1.0` (half the warehouse calls fail, and the
primary model always fails, so the fallback model answers).

### Docker (optional)
```bash
docker build -t retail-agent .
docker run -it --env-file .env -v "$HOME/.config/gcloud:/root/.config/gcloud:ro" retail-agent chat
```

## Example

```
you> Why are users in Texas underspending compared to California?
 ... (Querying the data warehouse...)
Texas customers spend about 9% less per customer than California ... (table) ... What I would look at next ...
trace 3f1c2a9b - 14.2s - $0.0104 - 2 SQL

you> Create a report for Q1 including insights and action items for Q2
Saved report #1 - Q1 2026 performance review ...

you> Delete all the reports we made in this conversation
I found 1 report that would be permanently deleted ... please confirm in the prompt below.
     Permanent deletion requested - all reports created in this conversation
     # | Title                       | Created
     1 | Q1 2026 performance review  | 2026-11-16 20:31
Type DELETE 1 to confirm, or press Enter to cancel: DELETE 1
Permanently deleted 1 report(s): #1.
```
Full real transcripts (online and offline), traces and eval output: [docs/EXAMPLE_RUN.md](docs/EXAMPLE_RUN.md).

## Framework choice

**Google ADK**. I have **production experience** with it. Why it fits this problem:
* **Plugins** apply cross-cutting policy to every model and tool call (input guard, PII redaction,
  budgets, tracing, graceful errors), so new tools inherit safety automatically.
* **Instruction providers** rebuild the system prompt each turn (persona, preferences, retrieved
  golden trios) without redeploys.
* Native **Gemini** support with HTTP retry options and a **FallbackModel**; **LiteLlm** for Ollama;
  pluggable session services; OpenTelemetry; deployable to Cloud Run or Vertex AI Agent Engine.
* Custom `BaseLlm` subclasses make deterministic offline tests of the *real* runner possible.

What I deliberately did **not** delegate to the framework: the deletion confirmation (ADK's tool
confirmation is experimental; ours is token-bound and deterministic) and SQL safety (enforced by an AST
guard, not by instructions). Details: [TECHNICAL_DESIGN §2](docs/TECHNICAL_DESIGN.md#2-technology-choices).

## Repository layout
```
src/retail_agent/
  agent/        engine (ADK App/Runner, error boundary), instructions, plugins, analysis (governed SQL), services
  safety/       sql_guard (AST rewrite, k-anonymity lineage, entitlements), pii, input_guard, policy
  warehouse/    BigQuery adapter (extends the provided BigQueryRunner), DuckDB adapter, resilience, chaos, synthetic data
  golden/       trio store, hybrid retriever, promotion gate + mining
  reports/      saved reports + two-phase deletion
  memory/       user preferences       persona/   registry + quality gate
  observability/ trace store + metrics  tools/     registry (extension point) + built-in tools
  llm/          model factory (Gemini + fallback, Ollama, offline models)   cli.py
config/         policy.yaml (PII / quasi / k), entitlements.yaml, personas/*.yaml
data/golden/    trios/*.yaml (14 seeded analyst trios), candidates/
evals/          cases.yaml (+ thresholds), run_evals.py
scripts/        build_offline_db.py, smoke_online.py
tests/          offline unit, adversarial and end-to-end tests
docs/           design documentation
```
