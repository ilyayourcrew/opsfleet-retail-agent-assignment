# Technical design

Companion to [ARCHITECTURE.md](ARCHITECTURE.md) (diagrams). This document explains how the system works
in production, why each technology was chosen, how every requirement is met, and how the prototype in
this repository maps onto the design. Paths like `src/retail_agent/safety/sql_guard.py` point to the
code that implements each part.

Contents
1. [Context and constraints](#1-context-and-constraints)
2. [Technology choices](#2-technology-choices)
3. [Components and data flow](#3-components-and-data-flow)
4. [Requirements, one by one](#4-requirements-one-by-one)
5. [Error handling and fallback strategies](#5-error-handling-and-fallback-strategies)
6. [Security model](#6-security-model)
7. [Sizing, latency and cost](#7-sizing-latency-and-cost)
8. [Deployment and operations](#8-deployment-and-operations)
9. [Prior art reviewed](#9-prior-art-reviewed)
10. [Limitations and next steps](#10-limitations-and-next-steps)

---

## 1. Context and constraints

Users are non-technical executives who ask open questions ("why did churn spike?"), follow up, and ask
for reports with action items. The agent works over `bigquery-public-data.thelook_ecommerce`
(`orders`, `order_items`, `products`, `users`) and a "golden" bucket of analyst trios
(question → SQL → report).

Binding answers from the client (Slack, see [ASSUMPTIONS.md](ASSUMPTIONS.md)):

| Topic | Constraint |
|---|---|
| PII | Never reaches the LLM, never shown: first/last name, email, street address, postal code, exact coordinates (IP address in `events` is listed as PII too). Age, gender, city, state, country, traffic source only in aggregates. Groups below **k = 5** hidden, configurable. Masking must be more robust than column-name matching. No DLP / policy tags today. |
| Golden bucket | Theoretical: we define the format and seed it. The approval flow must scale (no human bottleneck). |
| Scale | Tens to a few hundred executives, few concurrent; sizing basis 100 active users/day × 10 questions, ~1 saved report each, ~1M new rows/day. |
| Latency | Simple questions 10–30 s, long reports 1–2 min. |
| Budget | $1 per question, configurable. |
| Deletion | Hard delete, no recovery window. Reports private to their author; no admin delete/restore. Strict confirmation showing the exact list. |
| Persona | One non-developer edits it; changes pass an automated quality gate, no manual approval. |

## 2. Technology choices

| Building block | Choice | Why | Alternatives considered |
|---|---|---|---|
| Agent framework | **Google ADK (Python)** | Native Gemini support with built-in retry options and a `FallbackModel`; a **plugin system** (before/after run, model, tool and error callbacks) that lets safety, budgets and tracing apply to *every* tool without touching tool code; **instruction providers** (system prompt rebuilt per turn, which is what makes no-redeploy personas and per-user preferences cheap); session services (in-memory, SQLite, Vertex/Firestore-backed) and OpenTelemetry hooks; deploys unchanged to Cloud Run or Vertex AI Agent Engine. I have production experience with ADK (stated per the brief). | LangGraph: explicit graphs and interrupts are excellent, but tool calling over a fixed tool set plus cross-cutting policies maps more directly onto ADK plugins. A hand-rolled loop gives control but re-implements sessions, function-calling and tracing. |
| LLM | **Fallback chain of newest Gemini models**: `gemini-3.8-flash` (primary) → `gemini-3.5-flash` → `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite`; **light model** `gemini-3.5-flash-lite` for cheap side tasks (input-scope classifier, eval/promotion judge). All ids are configuration (`RA_MODEL_PRIMARY`, `RA_MODEL_FALLBACK`, `RA_MODEL_LIGHT`), verified against the live model list. | Flash-class models meet the 10–30 s budget with several tool round-trips and cost about 1 cent per question (see §7). *Different* models give real redundancy (separate quota and capacity). This mattered in live testing: on the free tier `gemini-3.8-flash` was returning 503/504 ("high demand") and `gemini-3.5-flash` 429 (quota), and the chain plus per-model circuit breakers kept the agent answering from Flash-Lite in 1–5 s per call. | Pro-class models for primary: better reasoning on very long reports but 3–5× the latency and cost. They can be enabled per request type later (route "report" intents to Pro). |
| Embeddings | Gemini embeddings (`gemini-embedding-001`, 768 dims) | Same provider and auth; semantic retrieval of golden trios. Optional: retrieval degrades to BM25 if unavailable. | Vertex text-embedding models (equivalent). |
| SQL safety | **sqlglot** AST parsing, qualification and rewriting | Real parser for BigQuery SQL: we can *rewrite* queries onto policy views, run column lineage for k-anonymity, enforce LIMIT, and transpile to DuckDB for offline mode. Regex or "SELECT-only" checks are bypassable. | BigQuery authorized views and row-access policies are the production backstop (§6); they don't replace AST checks because those also produce *actionable error messages* for self-correction. |
| Warehouse | **BigQuery** (on-demand), read-only service account, `maximum_bytes_billed` per query, dry-run first | Required by the brief; dry-run is free and gives syntax validation plus a byte estimate before any cost. | – |
| Golden bucket | **GCS** (versioned YAML objects) + **BigQuery Vector Search** index of trio embeddings + keyword index | GCS is the natural "data lake" for analyst artifacts; Vector Search keeps retrieval next to the data, with no extra service to run. | Vertex AI Vector Search (more scale than needed for thousands of trios), Conversational Analytics API "golden queries" (managed, but no control over PII enforcement or per-user entitlements). |
| App state | **Firestore** (reports, preferences, persona versions, pending actions, sessions via ADK) | Serverless, strongly consistent per document, TTL for pending actions, low ops for this scale. | Cloud SQL Postgres (better for full-text search; in Firestore search over report text is done with a small search index or BigQuery). |
| Compute | **Cloud Run** service `agent-api` (+ Cloud Run Jobs for gates and miners) | Stateless container, scales to zero, concurrency per instance, IAP in front. | Vertex AI Agent Engine (managed ADK runtime; a good option once its plugin support matches what we use). GKE (overkill). |
| Identity | IAP + Workspace / IdP groups → entitlements | Per-user data scoping needs a trusted identity. | – |
| Observability | OpenTelemetry → Cloud Trace / Logging; LLM traces to **Langfuse** (self-hosted on Cloud Run); log sink to BigQuery for metrics; Cloud Monitoring alerts | Standard GCP stack + an LLM-native trace UI for "what did the model see and say". | LangSmith, Arize (equivalent). |
| CLI | Typer + Rich | Required CLI; Rich renders markdown tables and confirmation prompts well. | – |

**Prototype stand-ins.** Firestore → SQLite (`var/app.sqlite`, FTS5 for report search); ADK sessions
→ `SqliteSessionService`; GCS + Vector Search → `data/golden/` YAML + BM25 + Gemini embeddings with RRF;
Cloud Trace / Langfuse → trace plugin writing spans to SQLite + JSONL with a CLI viewer; IAP → `--user`.
Offline mode swaps BigQuery for a schema-identical **DuckDB** copy (SQL transpiled by sqlglot) and Gemini
for **Ollama** (`LiteLlm`) or a deterministic **rule-based model**, so the same pipeline runs without
network or credentials.

## 3. Components and data flow

### 3.1 Components (prototype module → production service)

| Component | Code | Production |
|---|---|---|
| CLI / chat front | `cli.py` | CLI + web chat or Slack app, calling `agent-api` |
| Chat engine (ADK `App` + `Runner`, error boundary, timeout, confirmation path) | `agent/engine.py` | `agent-api` on Cloud Run |
| Instruction provider (persona + preferences + entitlement + schema + golden examples + rules) | `agent/instructions.py` | same |
| Plugins: observability, guardrails | `agent/plugins.py` | same, exporting OpenTelemetry |
| Tools (registry = extension point) | `tools/registry.py`, `tools/builtin.py` | same |
| Governed SQL execution | `agent/analysis.py`, `safety/sql_guard.py`, `safety/pii.py` | same + BigQuery authorized views / row-access policies |
| Input guard | `safety/input_guard.py` | same (Flash-Lite classifier) |
| Policy & entitlements | `config/policy.yaml`, `config/entitlements.yaml`, `safety/policy.py` | Firestore / IdP groups |
| Warehouse adapters | `warehouse/bigquery_backend.py` (extends the provided `BigQueryRunner`), `warehouse/duckdb_backend.py`, `warehouse/resilient.py` | BigQuery |
| Golden knowledge | `golden/store.py`, `golden/retriever.py`, `golden/promotion.py` | GCS + BigQuery Vector Search + Cloud Run Jobs |
| Saved reports + deletion | `reports/store.py` | Firestore |
| Preferences | `memory/preferences.py` | Firestore |
| Personas | `persona/registry.py`, `persona/quality.py` | Firestore + admin page + Cloud Run Job gate |
| Traces & metrics | `observability/trace_store.py`, `observability/metrics.py` | Cloud Trace/Logging, Langfuse, BigQuery log sink, Cloud Monitoring |
| Evaluation | `evals/run_evals.py`, `evals/cases.yaml`, `tests/` | CI (Cloud Build) + nightly online eval job |

### 3.2 Request lifecycle (one analysis question)

1. **Identity.** IAP authenticates the user; the user id reaches the agent as the ADK `user_id`. Tools
   read identity only from `ToolContext`, never from model-supplied arguments.
2. **Turn start** (`ObservabilityPlugin.before_run_callback`). A `TurnState` is created with
   `trace_id` = ADK invocation id.
3. **Input guard** (`GuardrailsPlugin.before_run_callback`). Heuristics, then the Flash-Lite scope
   classifier. A blocked message ends the turn immediately with a polite refusal, with no model call or
   SQL. Implicit preference signals ("too long", "as a table") are recorded here.
4. **Instruction build** (`instructions.py`, once per invocation), ordered **static first** so Gemini's
   implicit prefix cache can serve the shared part:
   * role, compact schema with aggregate-only markers, operating rules and the non-negotiable safety
     rules (identical for every user and question);
   * then the active persona (changes weekly);
   * then the per-user and per-question parts: preferences, entitlement scope and date, the **top-k
     golden trios retrieved for this message** (report figures masked), and recent confirmed actions
     (e.g. deletions).

   Cached tokens are recorded on every model span.
5. **Model ↔ tools loop** (ADK). Gemini plans and calls `run_analysis_sql` one or more times (multi-step
   questions), possibly `create_chart`, `save_report`, etc. Every model call passes the budget check
   (`before_model_callback`) and is traced (message delta in, parts out, tokens, latency, model version).
6. **Governed SQL** (`analysis.py`). Guard rewrite → cache lookup → BigQuery dry-run (syntax + bytes) →
   byte-cap check → execute with `maximum_bytes_billed` → PII value scan → pseudonymise customer ids →
   compact JSON (≤ 50 rows + numeric summary) back to the model. Errors and empty results come back as
   structured payloads with hints and remaining attempts (§4.5).
7. **Output guard** (`after_model_callback`). Every model text part is scanned and redacted before it
   is stored or shown; tool results are scanned before the model sees them (`after_tool_callback`).
8. **Turn end.** The trace (turn row + spans) is persisted; the answer, cost, latency and trace id are
   returned to the UI. If the turn requested a deletion, the UI renders the confirmation step (§4.3).

### 3.3 Where data lives and how it moves

* **Raw rows never leave BigQuery** except as the result of a guarded query. Result sets are capped
  (200 rows, 50 shown to the model), PII-free by construction, scanned, pseudonymised, and held in
  memory only (per-session result cache for charts, 10 latest results).
* **What the LLM sees:** instructions, conversation history, *guarded* result samples and tool messages.
  No PII column can be selected, and value-level scanning covers anything unexpected.
* **What is persisted:** conversation events (ADK session store), saved reports (owner-scoped), user
  preferences, persona versions, pending deletion tokens (TTL), audit log of report actions (ids and
  counts only), traces. Traces are a data sink too, so they are **PII-scrubbed at write time** (user
  questions, model text, tool arguments and previews) and never contain saved-report bodies (only
  their length); a deleted report therefore does not live on in the logs.
* **Golden trios** live in GCS (prototype: `data/golden/trios/*.yaml`), embeddings in BigQuery
  (prototype: `var/golden_embeddings.json` cache keyed by content hash).

## 4. Requirements, one by one

### 4.1 Hybrid intelligence: the golden bucket (R1)

**Trio format** (`golden/store.py`, examples in `data/golden/trios/`):

```yaml
id: state_spend_comparison
question: Why are users in Texas underspending, and how do they compare to users in California?
intent: segment_comparison_geo
tags: [state, underspending, spend, compare, ...]
tables: [order_items, users]
sql: |            # BigQuery SQL that answered it
  SELECT u.state, COUNT(DISTINCT u.id) AS customers, ...
interpretation:   # how the analyst read the question - the transferable part
  - '"Underspending" = lower revenue per customer. Decompose: orders/customer x items/order x item price.'
analyst_report: | # what the analyst concluded and recommended
  Texas customers spend ~20% less per customer ...
author: analytics-team   source: analyst | mined   status: active | candidate | deprecated
quality_score: 0.9   version: 1   created_at / last_validated_at
```

**Query time (how relevant knowledge is provided).**
1. The user message is embedded (Gemini, task `RETRIEVAL_QUERY`) and matched against trio embeddings
   (production: `VECTOR_SEARCH` in BigQuery; prototype: cosine over cached vectors) **and** scored with
   BM25 over question + intent + tags + interpretation notes, with a light retail synonym map.
2. Rankings are fused with Reciprocal Rank Fusion (robust to different score scales). A minimum
   lexical or semantic relevance is required, so unrelated questions get **no** examples rather than
   misleading ones (tested).
3. The top 3 trios are injected into the system instruction as "how our analysts approached similar
   questions": interpretation notes, SQL and a report excerpt **with figures masked** ("~N% less"),
   with an explicit instruction to *adapt*, not copy. Historical figures are stale and company-wide,
   possibly outside the reader's product entitlement, and they were seen being copied into new
   answers. The excerpt is there to teach reasoning and recommendations, not numbers. This costs no extra model round-trip. For later sub-questions in a multi-step analysis
   the model can call `search_golden_knowledge`.
4. The retrieved trio ids are logged on the trace, so we can measure which trios get used and whether
   answers that used them are rated better.

Effect: the agent inherits metric definitions (net revenue excludes Cancelled/Returned), decomposition
patterns ("underspending" = frequency × basket × price) and interpretation ("compare like-for-like
YTD"), not only SQL syntax.

**Updating the bucket over time (scales without a human bottleneck).** `golden/promotion.py`
* *Sources:* (a) analysts drop new trios into `candidates/` (GCS prefix), (b) **mining**: turns with
  positive feedback and successful SQL become candidate trios (question + final working SQL + accepted
  answer), `retail-agent learn`.
* *Automated gate* for every candidate:
  1. schema check;
  2. the SQL passes the guard (safe, read-only, PII-free);
  3. it executes and returns rows;
  4. it is not a duplicate (same SQL or near-identical question);
  5. it is **prompt-safe**: the question, interpretation and report text pass the same injection and
     PII heuristics as user input;
  6. an LLM judge (Flash-Lite) scores "does this SQL answer this question?" ≥ 0.7.

  Pass means promoted to `active`; fail means it stays a candidate with reasons. The prompt-safety
  check matters because an automated learning loop is a write path into *other users'* system
  prompts. Without it, a user could rate their own crafted conversation and plant instructions for
  everyone.
* *Continuous re-validation* (nightly job, `learn --revalidate`): every active trio is re-run; trios
  broken by schema drift are automatically `deprecated`.
* *Humans review exceptions only:* the analytics team gets a weekly digest of rejected candidates and
  newly promoted mined trios and can veto (set `deprecated`). Usage counts and feedback per trio drive
  `quality_score`, which can weight retrieval.
* Production wiring: Pub/Sub `feedback` events → Cloud Run Job miner → GCS `candidates/` → gate job →
  GCS `trios/` + embedding upsert into the BigQuery vector index.

### 4.2 Safety and PII masking (R2)

Defence in depth: each layer assumes the previous one failed.

| Layer | What it does | Code |
|---|---|---|
| 1. Identity and entitlements | User comes from IAP/`ToolContext`, never from the model. Unknown users have no data access (fail closed). | `safety/policy.py`, `config/entitlements.yaml` |
| 2. Input guard | Heuristics (prompt injection, explicit SQL statements, requests for personal data) fail **closed** and are deliberately **high-precision**: refusing a legitimate question ("why did revenue *drop*?") is a product failure, and data cannot be modified anyway; Flash-Lite scope classifier (off-topic / malicious) fails **open** if unavailable, because data-layer controls still apply. Blocked turns never reach the model. | `safety/input_guard.py` |
| 3. Instructions | Scope and privacy rules declare precedence over persona and preferences, and the persona gate rejects persona text that tries to change rules. Prompt position is not relied on for safety (layers 4–8 are code), so the prompt is ordered for caching. | `agent/instructions.py` |
| 4. **SQL guard (structural)** | Single read-only `SELECT`; DML/DDL/scripting nodes denied anywhere; allowlist of the four tables (no `INFORMATION_SCHEMA`, wildcards, other datasets, table functions, exfiltration functions); CTEs may not shadow table names. **Every table is rewritten into a policy sub-query that physically lacks PII columns** and carries the user's entitlement filter; the query is then qualified against that PII-free schema, so `SELECT *`, `u.*`, `TO_JSON_STRING(u)` cannot reach PII. Whole-row struct references are rejected. | `safety/sql_guard.py` |
| 5. Quasi-identifiers and k-anonymity | Column lineage over sqlglot scopes tracks which outputs carry raw demographics and customer ids. Raw demographics may not appear in final rows; any aggregation that *touches* demographics (keys, values or filters, directly or via a sub-query/CTE) gets `HAVING COUNT(DISTINCT customer_id) >= k` injected (k = 5, `RA_K_ANONYMITY_MIN` / `policy.yaml`). Row-level results filtered by demographics are rejected. Two classic bypasses are closed explicitly: **list-building aggregates** (`ARRAY_AGG(STRUCT(id, city, age))`, `STRING_AGG`, `APPROX_TOP_COUNT`, `APPROX_QUANTILES`) may not carry customer ids or demographics, because they enumerate each member of a "k-anonymous" group; and **singling out**: in a query that uses demographics, customer ids may not be compared with literals (`MAX(CASE WHEN id = 123 THEN age END)` passes k but reveals one person). | `safety/sql_guard.py` |
| 6. Value-level scanning | Result sets are scanned by **content shape**, not column name: e-mails, phones, street addresses, coordinate pairs, IPs, Luhn-valid card numbers, columns whose values mostly look like postal codes or raw coordinates. Matches are masked before the model sees them. | `safety/pii.py` |
| 7. Pseudonymisation | Customer ids in results are replaced by keyed HMAC tokens (`C-1a2b3c4d`), stable for follow-ups, irreversible without the secret. | `safety/pii.py` |
| 8. Output guard | Every model text part, every saved report and every tool result is redacted again before display or storage. | `agent/plugins.py`, `tools/builtin.py` |
| 9. Production backstop | BigQuery **authorized views** without PII columns plus **row-access policies** per entitlement group; the agent's service account has no access to base tables. Optionally BigQuery aggregation-threshold analysis rules for k-anonymity at the engine level, and Sensitive Data Protection (DLP) inspection on outputs for names. | design |

**Per-user product scoping.** "Products related to him" is modelled as entitlements on product
`department`, `category` and `brand` (AND across dimensions, OR within a list). The guard restricts
`products` to entitled products, `order_items` to items of those products, `orders` to orders that
contain such items, and `users` to customers who bought them, so *every* metric (revenue, customers,
demographics) is computed only over the user's slice. Tested in `tests/test_safety.py` and
`tests/test_agent_e2e.py`.

**Analysis-only scope.** Off-topic requests are refused by the classifier and by instructions; the
agent has no tools other than analysis, reports, preferences and charts.

Known limits (documented, not hidden): k-anonymity per query does not stop *differencing* attacks
across many queries (total minus "total without X"), nor singling out through other near-unique
values (an exact timestamp). Two facts bound the risk: direct identifiers are physically unreachable,
so the worst case is learning a demographic attribute of a *pseudonymous* customer; and the users are
authenticated internal executives with audited queries. Production mitigations, in order of
strength: BigQuery **aggregation threshold analysis rules** (k enforced by the engine, per privacy
unit), BigQuery **differential privacy** (`SELECT WITH DIFFERENTIAL_PRIVACY`, noise plus a per-user
privacy budget, the only formal defence against differencing), query auditing and per-user rate
limits. Person-name detection in free text relies on structural removal (names are never selectable)
rather than NER; DLP inspection is the production add-on.

### 4.3 High-stakes oversight: destructive operations (R3)

Principle: **the model can only ask for a deletion; it can never perform one.**

* `request_report_deletion(scope, text_query, report_ids)` resolves the exact set, **owner-scoped** at
  the repository layer (every query has `WHERE owner_id = ?`):
  `this_conversation` (reports whose `session_id` is the current conversation), `matching`
  ("mentioning Client X": SQLite FTS5 phrase search over title and content; Firestore + search index in
  production), `ids`, or `all`.
* It stores a **pending action**: exact report ids, a **snapshot hash** of their content, owner,
  session, a random single-use token and a **5-minute TTL**. A new request supersedes the previous one.
* The UI (not the model) renders the exact list (id, title, created) with a "permanent" warning and asks
  the user to type **`DELETE <n>`**. Only then does deterministic code call
  `ReportStore.confirm(token, owner, session, typed)`, which re-checks: token valid and unused,
  same owner, same session, not expired, phrase exact, reports still exist and are unchanged
  (snapshot). Then it runs a transactional hard delete and writes an audit record. Single use is an
  atomic compare-and-set (`UPDATE ... WHERE status = 'pending'` inside `BEGIN IMMEDIATE`), so two
  concurrent confirmations cannot both execute.
* **"Hard delete, no recovery" is meant literally.** A plain `DELETE` in SQLite (and most databases)
  only marks pages free; the text stays readable in the file until overwritten. The store runs with
  `PRAGMA secure_delete = ON` and FTS5 `secure-delete`, so the bytes are zeroed and the search index
  forgets the tokens (a test reads the raw database file to prove it). The audit log keeps ids and
  counts, never titles or content; traces never store report bodies. In production the equivalent is
  a Firestore document delete (no soft-delete layer) with backups excluded or expiring within the
  retention window the client accepts. Conversation transcripts follow the session retention policy
  (default 30 days), which is a separate decision from report deletion and is called out to the client.
* A wrong phrase or Enter cancels (and consumes the token). Expired, stale or cross-user tokens are
  rejected with a clear message. The next instruction tells the model what happened ("reports #3, #4:
  DELETED (confirmed by the user)") so the conversation stays coherent.
* UX: one extra keystroke-level step, inline in the chat; no mode switch; nothing happens on ambiguity.

Why not ADK's built-in tool confirmation alone? It is marked experimental, and a reported issue in
the Java port lets a confirmation reply that matches *any* earlier request resume execution. Binding
the confirmation to a token, user, session, exact snapshot and TTL in our own code makes the guarantee
independent of framework behaviour. Tests: `tests/test_agent_e2e.py` (wrong phrase, reused token,
other user, other conversation untouched, stale snapshot, expiry).

### 4.4 Continuous improvement: the learning loop (R4)

**User level (4a)**, `memory/preferences.py`
* Dimensions: `format` (table | bullets | prose), `depth` (brief | standard | deep), `visuals`
  (charts | text), plus free-text `note`s ("always compare to last year").
* **Explicit:** the user states it, the model calls `remember_preference` (or `/prefs`). Confidence 1,
  always wins.
* **Implicit:** deterministic signals in user messages and feedback comments ("too long", "more
  detail", "as a table", "show me a chart") add evidence; an inferred preference becomes active after
  **2 consistent observations** and never overrides an explicit one.
* Active preferences are rendered into every turn's instruction, so User A gets tables and User B
  bullets from the same agent.
* Production: Firestore per user, plus a weekly job that summarises each user's feedback and
  interaction patterns (answer lengths they engage with, follow-up "why" frequency, chart usage) with
  Flash-Lite into preference updates, still subject to the explicit-wins rule.

**System level (4b)**
* Every turn is traced with outcome signals: SQL failures, self-corrections, empty results, guard
  blocks, cost, latency, feedback.
* **Good interactions become knowledge:** positively rated turns are mined into candidate trios and
  pass the automated gate (§4.1), so the next user benefits.
* **Failures become fixes:** a weekly job clusters failed and negatively rated turns by error type and
  by question embedding. Recurring patterns turn into (a) new or fixed trios, (b) schema/metric notes
  in the instruction, (c) new eval cases. Every such change ships only if the eval gate passes (§4.6),
  which closes the loop safely.

### 4.5 Resilience and graceful error handling (R5)

**SQL self-correction without cost inflation** (`agent/analysis.py`)
1. The guard catches syntax and policy problems before anything runs (free) and returns a *specific*
   message and hint ("Personal data columns are not available: email → analyse in aggregate ...").
2. BigQuery **dry-run** (free) catches semantic errors (unknown column, bad signature) and reports
   bytes; queries above the byte cap are rejected *before* running, with a narrowing hint.
3. **Empty results** come back as `status: empty` with diagnostics: closest valid values for string
   filters (e.g. `brand = 'levis'` → `["Levi's"]`, computed through the same guard and entitlements),
   the data's date range, and privacy/entitlement notes when those can explain emptiness.
4. The model gets `attempts_left`; after **3 failed attempts** per question the tool returns `give_up`
   and the model must explain instead of looping. Infrastructure failures (warehouse down) do **not**
   consume correction attempts and tell the model to stop.
5. Budgets cap everything else: $ per question (tokens × price + bytes × price), max model calls and
   max tool calls per turn, a turn timeout, and a result cache so repeated queries cost nothing.

**Third-party failures**
| Dependency | Strategy |
|---|---|
| Gemini (429/5xx/timeouts) | ADK `FallbackModel` over a chain of different models. Each non-final model sits behind a `BreakerLlm` that streams the inner call and applies **two clocks**: a **time-to-first-token deadline** (20 s; no token = overloaded or hung, so fall back, which status-code-based fallback alone would not do) and a **total cap** (60 s) once tokens flow, so a long report that is being written is not thrown away (a single fixed deadline did exactly that in live testing) and a **process-wide circuit breaker** per model that trips immediately on quota (429) or deadline errors and after 2 consecutive 5xx, so later turns skip a sick model with zero latency and probe it again after 60 s. Non-final models retry 5xx once and never retry 429; the final model has the full retry policy. In production the breaker state would be shared across instances (Memorystore) or fed by a health checker. If **all** models fail: `on_model_error_callback` returns a graceful message plus, when a golden trio matches strongly, a **degraded answer**: the trio's SQL run through the full guard and rendered as a table. The user still gets data. |
| BigQuery | Typed error classification (syntax / cost / timeout / transient / auth); transient errors retried with jittered backoff; **circuit breaker** fails fast while BigQuery is down and half-opens to probe recovery. Every job has a **server-side `job_timeout_ms`** and is **cancelled** when the client stops waiting, because a client timeout alone leaves the job running and billing. The job id is recorded on the trace span. |
| Classifier / embeddings | Optional enhancers with their own breakers: the classifier fails open (heuristics stay), embeddings fall back to BM25. |
| Any tool exception | `on_tool_error_callback` converts it into a structured error the model can reason about. |
| Anything else | `ChatEngine.ask` is the error boundary: timeouts and unexpected exceptions become a friendly message with a trace reference; the CLI never crashes and the session is intact. |

Fault injection is built in (`--chaos`, `--llm-chaos`, `ChaosWarehouse`, `ChaosLlm`) and covered by
tests and the eval suite.

### 4.6 Quality assurance (R6)

Summary here; full detail and results in [EVALUATION.md](EVALUATION.md).
* **Before deployment:** unit and adversarial tests (guard, PII, entitlements, deletion), offline
  end-to-end scenario tests through the real ADK runner (scripted model), and the **eval suite**
  (`evals/run_evals.py`) as a CI gate with thresholds: golden execution accuracy, LLM-judge score,
  red-team pass rate = 100%, behaviour and resilience pass rates, p95 latency, **zero PII leaks**. The
  same suite runs against Gemini + BigQuery before each release and gates persona and prompt changes.
* **Does the report answer the intent?** (1) Execution accuracy against analyst trios for paraphrased
  questions (do the agent's numbers match the reference result?); (2) **held-out** questions that no
  trio covers, with their own reference SQL, so the score measures generalisation and not copying of
  a retrieved example, plus a `--no-golden` **ablation** that measures what the golden bucket adds;
  (3) an LLM judge (different model from the agent) scores intent, grounding, actionability and
  clarity; (4) a deterministic **numeric grounding** check on every turn: each number in the answer
  must come from this turn's results or a simple derivation of them (`observability/grounding.py`);
  (5) in production, a sample of live reports is judged nightly and low scores are routed to analysts.
  Thumbs up/down and "rephrase rate" are the user-side signals.
* **UX:** task success (answer without rephrasing), turns-to-answer, latency p50/p95 against the 10–30 s
  and 1–2 min targets, feedback rate, refusal rate on legitimate questions (false positives), deletion
  confirmation completion and abandonment, plus periodic moderated sessions with 3–5 executives on
  scripted tasks.

### 4.7 Observability (R7)

Summary here; metric definitions, alerts and debugging workflow in [OBSERVABILITY.md](OBSERVABILITY.md).
* One **trace per turn** (`trace_id` = ADK invocation id) with spans for the input guard, every model
  request (the *message delta* the model received), every model response (text / function calls,
  tokens, model version, latency), every SQL attempt (original SQL, rewritten SQL, error type, rows,
  bytes, cache hit), guard decisions, redactions, errors and budget stops.
* The turn row aggregates counters for metrics: status, latency, model/tool calls, SQL attempts,
  failures, empties, self-corrections, guard blocks, PII redactions, tokens, $ cost, fallback and
  degraded flags, persona version, golden trio ids.
* `retail-agent trace [id]` shows the full correspondence of a turn; `retail-agent stats` shows agent
  metrics. Production exports the same spans via OpenTelemetry to Cloud Trace and Langfuse; metrics
  come from a BigQuery log sink with Cloud Monitoring alerts.

### 4.8 Agility: persona management (R8)

* The persona holds **tone, style rules and report structure only** (`config/personas/*.yaml`).
  A persona cannot weaken the safety rules: they are enforced in code, the prompt's safety section
  declares precedence over the persona, and the gate rejects persona text that tries to change rules.
  The lint targets *actions* ("delete reports", "show e-mails"), not single words. "Call out any
  revenue drop" is a normal tone rule and passes, because a non-developer cannot debug a false
  rejection.
* The editor changes a YAML file (production: a small admin form or a Google Sheet) and runs
  `retail-agent persona publish <file>`. The **automated quality gate** runs: schema limits, policy lint
  (rejects attempts to override rules, request personal data, mention data changes, links), then a
  **behavioural check** that runs canned conversations through the real agent *with the candidate
  persona* in a sandbox (answers present, no PII, privacy refusal still works, a report still contains
  action items). Pass: a new immutable version becomes active; the **next message of every user** uses
  it, because the instruction is rebuilt per turn. No restart or redeploy. Fail: the current persona
  stays live and the editor sees why.
* `persona rollback` and `persona history` give instant undo and an audit trail.
* Per-audience personas (CEO vs category leads) are supported by design as optional per-role
  overrides selected from the entitlement role; the default is one global persona (client did not
  require it).

### 4.9 Extensibility

New capabilities are tools: a typed Python function with a docstring, registered with
`@register(name, group)` in `tools/registry.py`. They automatically get tracing, budgets, PII
scanning of outputs, error containment and identity from `ToolContext`. `create_chart` (matplotlib
PNG from a previous query result) ships as the worked example. `email_report` (Gmail API / SendGrid,
with the same two-phase confirmation pattern as deletion, because sending is outward-facing) and
`web_trends` (Google Search grounding tool in a sub-agent, so web content never mixes with the data
tools' context) are designed but not built.

## 5. Error handling and fallback strategies

| Failure | Detection | Response | User sees |
|---|---|---|---|
| Malformed / unsafe SQL | sqlglot parse, guard rules | structured error + hint, attempt counted | nothing; the agent self-corrects |
| Unknown column / bad function | BigQuery dry-run (free) | same | same |
| Query too expensive | dry-run bytes > cap | `cost` error with narrowing hint | same |
| Empty result | 0 rows | diagnostics (valid values, date range, privacy note) | same, or an explanation |
| 3 failed attempts | counter per turn | `give_up` | short explanation + rephrasing suggestion |
| BigQuery 5xx / rate limit | typed classification | jittered retries, then circuit opens | "data warehouse temporarily unavailable, try again shortly" |
| Gemini 429 / 5xx | status code | retries, then fallback model | normal answer (trace flags `fallback_used`) |
| All models down | `on_model_error_callback` | degraded mode: closest golden analysis through the guard | apology + useful table |
| Tool exception | `on_tool_error_callback` | structured error to the model | the agent works around it |
| Cost / call budget hit | before_model / before_tool | stop with partial findings | "stopped to stay within the cost limit" |
| Turn timeout / unexpected exception | engine boundary | friendly message, trace reference | the CLI keeps running |
| Classifier / embedding outage | circuit breakers | heuristics only / BM25 only | nothing |

## 6. Security model

* **Least privilege:** the agent's BigQuery service account can read only authorized views (prod) and
  has no write permissions; app storage is separate. The prototype adds structural SQL enforcement on
  top.
* **Trust boundaries:** user text, model output, tool results and data values are untrusted. Identity,
  entitlements, deletion confirmation and policy are trusted code paths the model cannot influence.
* **Secrets:** API keys and the pseudonymisation HMAC key come from env / Secret Manager, never from the
  repo (`.env` is git-ignored). With no key configured, a random key is generated once per
  installation (`var/pseudonym.key`). A default key in a public repo would make every pseudonym
  reversible, because customer ids are small integers that can all be hashed.
* **Terminal output is untrusted text:** report titles, SQL and user input are escaped before the CLI
  renders them, so model-written text cannot alter what the confirmation step shows or crash it.
* **Supply chain:** the tested dependency set is pinned, exactly for ADK and sqlglot because the safety
  logic relies on their internals. Each upgrade is its own PR through the eval gate (proposed
  automatically by Dependabot or Renovate in production).
* **Audit:** report saves, deletion requests, confirmations, cancellations and denials are logged with
  user and session; persona publications are versioned with author.

## 7. Sizing, latency and cost

* **Load:** about 1,000 questions/day and about 100 report generations/day, peaks of a few concurrent
  users. One Cloud Run instance (min 1 to avoid cold starts, max 5) is plenty; BigQuery on-demand.
* **Data growth:** about 1M new rows/day. Production tables should be **partitioned by
  `DATE(created_at)`** and clustered (`order_items` by `product_id, status`; `orders` by `user_id`).
  Analyst SQL always filters dates, so scans stay small. On the public dataset today every golden query
  scans 3–10 MB.
* **LLM cost per question (Flash-class, list prices at writing):** about 3 model calls × about 6k input
  tokens + about 1k output tokens ≈ **$0.01**; reports with 4–6 queries ≈ $0.03–0.05. BigQuery ≈ $0.0001
  per query at these scan sizes. Monthly: about $300–400 LLM + under $5 BigQuery at 1,000 questions/day.
  The $1/question budget (`RA_BUDGET_USD_PER_QUESTION`) is therefore a safety net enforced per turn.
* **Latency budget:** simple question = input guard (≤ 1 s) + 2–3 model calls (2–5 s each) + 1–2
  queries (1–3 s) ≈ **8–20 s**; reports ≈ 4–6 queries + longer generation ≈ **40–90 s**. The turn timeout
  (150 s) and call budgets bound the worst case. The instruction is ordered static-first for Gemini's
  implicit prefix caching (cached input tokens are billed at a fraction and processed faster). The
  share of cached input tokens is reported by `stats` and the eval summary.

## 8. Deployment and operations

* **Build:** in this repository, GitHub Actions (`.github/workflows/ci.yml`) runs:
  * on every push and PR: ruff, `pytest` on Python 3.11/3.12/3.14, and the offline eval gate;
  * on demand: the online eval gate.

  Dependency upgrades go through the same gate as separate PRs. In GCP production the same steps
  run in Cloud Build on every PR; merges to
  `main` build a container (Artifact Registry) and deploy `agent-api` to a staging Cloud Run service,
  then run the online eval suite against staging; promotion to prod requires the gate to pass.
  Terraform manages Cloud Run, IAP, service accounts, BigQuery authorized views, row-access policies,
  Firestore, Pub/Sub, Scheduler and Secret Manager.
* **Config without redeploy:** personas (Firestore + gate), entitlements (IdP groups), golden trios
  (GCS + gate), policy thresholds (`k`, budgets) are data, not code.
* **Runbook signals:** see [OBSERVABILITY.md](OBSERVABILITY.md) (error rate, fallback rate, guard block
  spikes, cost per question, p95 latency, PII redactions > 0 in final answers = page).

## 9. Prior art reviewed

A web search did not surface public solutions of this exact assignment. Related work I reviewed:

| Source | What I took | What I did differently, and why |
|---|---|---|
| Google Conversational Analytics API ("authored context", golden queries) | Curated question → SQL examples as the strongest steering signal; the trio idea fits this pattern. | Managed and opaque for our needs: no per-user product entitlements, no k-anonymity, no custom destructive-op flow. Kept retrieval in our code. |
| ADK / LangGraph BigQuery demo agents (e.g. selecta, chat-your-data), LangChain SQL toolkits | Tool-calling loop over a run-SQL tool, schema-in-prompt. | Most rely on prompt instructions and "SELECT-only" checks for safety and allow unbounded retries. Prompt-only safety is bypassable and unbounded retries inflate cost, so this design uses AST rewriting onto PII-free views, lineage-based k-anonymity, and capped attempts and budgets. |
| Gemini CLI BigQuery extension | Natural-language BigQuery for technical users. | Different audience (engineers), no governance layer. |
| ADK tool confirmation (HITL) | The idea of pausing on destructive tools. | Experimental, and a known issue in another port resumes on any matching confirmation, so deletion uses our own token-bound two-phase commit. |

## 10. Limitations and next steps

* The rule-based offline model is a test vehicle, not an analyst; offline quality comes from Ollama or,
  in practice, Gemini.
* The k-anonymity lineage covers the SQL shapes analysts use (joins, CTEs, sub-queries, unions,
  windows). Exotic constructs that the qualifier cannot resolve are rejected (fail closed), and the
  model self-corrects. Production adds engine-level enforcement (§4.2 layer 9).
* Per-query k-anonymity does not prevent differencing across queries (mitigations listed in §4.2).
* Golden trios steer interpretation, but a model can over-reuse a trio's *recommendations* (observed in
  the Q1 report example: data-derived KPIs alongside action items echoing the seeded analyst report).
  Mitigations: trio excerpts limited to interpretation plus a short report snippet, an explicit "adapt,
  don't copy" rule, the judge's grounding criterion and the numeric grounding check (every number is
  traced to a result; qualitative claims are not checked yet).
* Next: route report intents to a Pro model, prompt caching of the static instruction prefix, streaming
  answers to the CLI, a web chat UI, an admin page for personas and trios, and per-trio quality scores
  learned from feedback.
