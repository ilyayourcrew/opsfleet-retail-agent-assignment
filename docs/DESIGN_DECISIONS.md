# Design decisions: rationale, trade-offs and how to defend them

This is the "why" companion to [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md). It covers:
- each major decision, in short ADR form: the decision, the constraint that drove it, the
  alternatives, why this option won, what it costs, the evidence in this repository, and what would
  change the decision;
- the questions a reviewer is likely to ask, with short answers;
- what a tech-lead review of the first version found and fixed;
- a prioritised roadmap.

Rule of thumb when defending any choice: **name the constraint first, then the alternative you
rejected, then the evidence.** "I chose X" is weak. "The client said PII must never reach the
LLM, so prompt rules were not enough. I rewrite the SQL onto PII-free views, and here is the test
that tries 20 ways to smuggle PII" is strong.

---

## 1. Decision log

### D1. Google ADK as the agent framework
* **Constraint.** The safety, budgets and tracing policies must apply to every model and tool call,
  including tools added later. Personas and preferences must change without a redeploy. Gemini is
  required.
* **Alternatives.** LangGraph; a hand-rolled function-calling loop; the Vertex AI Conversational
  Analytics API.
* **Why ADK.** Plugins are a single choke point for cross-cutting policy: input guard, PII
  redaction, budgets and traces live in two plugins, and every tool inherits them. Instruction
  providers rebuild the system prompt each turn (persona, preferences, retrieved trios). ADK also
  gives native Gemini support with retry options and a `FallbackModel`, `LiteLlm` for Ollama,
  pluggable session stores and an OpenTelemetry export. I have production experience with it,
  which lowers delivery risk.
* **Cost.** Some of what I rely on is marked experimental (`FallbackModel`). The plugin
  early-exit semantics are subtle: the first plugin that returns a value stops the chain, and an
  early-exit event is authored as `model` (see finding F5). Pin the version and cover the behaviour
  with tests.
* **Evidence.** The end-to-end tests run the real ADK `Runner` with a scripted model
  (`tests/test_agent_e2e.py`).
* **What would change it.** If the flow became a fixed multi-stage pipeline with human approval
  steps in the middle, LangGraph's explicit graph and interrupts would fit better. If the client
  standardised on Vertex AI Agent Engine, ADK deploys there unchanged.

### D2. One analyst agent with tools, not a multi-agent system
* **Constraint.** 10–30 s latency target; every hand-off between agents costs one or more model
  round-trips. The tasks (query, interpret, report) share the same context.
* **Alternatives.** Planner, SQL writer and reporter agents; a separate router agent.
* **Why.** Fewer model calls means lower latency, cost and failure surface. Multi-step reasoning
  happens inside one tool-calling loop: the model issues several `run_analysis_sql` calls.
  Specialisation is achieved with tools and retrieved trios, not with extra agents.
* **Cost.** One prompt carries all the rules. Very long reports would benefit from a "report
  writer" pass on a stronger model.
* **What would change it.** Web search. It belongs in an isolated sub-agent so untrusted web text
  never shares context with the data tools (designed in TECHNICAL_DESIGN §4.9). Long reports could
  also be routed to a Pro model.

### D3. Model choice: a chain of Flash-class Gemini models, plus a light model for side tasks
* **Constraint.** Latency, the $1/question budget, free-tier rate limits, and the requirement to
  survive API downtime.
* **Alternatives.** A Pro model as primary; a single model with retries only.
* **Why.** Flash is fast enough for 2–4 tool round-trips inside the latency budget at about $0.01
  per question. A chain of *different* models gives real redundancy, because each model has its own
  quota and capacity. Live testing proved it: the primary returned 503/504 and the next model 429,
  and the agent kept answering from Flash-Lite. The classifier and judge use a cheaper model, and a
  different one from the agent, which avoids self-grading bias in evals.
* **Cost.** Lighter fallback models write weaker SQL. The self-correction loop and the trios absorb
  most of that, and traces flag `fallback_used`, so quality per model is measurable.
* **Evidence.** The online eval passed while served mostly by fallbacks (EVALUATION.md).
  `test_slow_model_times_out_into_fallback_and_breaker_skips_it`.

### D4. Safety enforced in code: a SQL AST guard that rewrites queries onto PII-free policy views
* **Constraint.** PII "never reaches the LLM and is never shown", masking "more robust than
  column-name matching", and malicious users.
* **Alternatives.**
  * Prompt rules only. Bypassable by prompt injection.
  * A regex / "SELECT-only" check. Bypassable with CTEs, `SELECT *`, `TO_JSON_STRING(u)`.
  * BigQuery authorized views only. This is the production backstop, but alone it gives the model
    no actionable errors, needs one view per entitlement combination, and does not run offline.
* **Why.** The guard parses with sqlglot and rejects anything that is not a single read-only
  SELECT. It then qualifies the query against a schema that *does not contain* PII columns and
  replaces every table with a sub-query that physically lacks them and carries the user's
  entitlement filter. PII is therefore unreachable by construction, not by detection. Value-shape
  scanning, HMAC pseudonyms and output redaction are additional layers that each assume the
  previous layer failed.
* **Cost.** The guard is security-critical code that must be maintained as the SQL dialect grows.
  Exotic SQL is rejected (fail closed) and the model rewrites it.
* **Evidence.** `tests/test_safety.py` (DML/DDL, smuggling via CTEs, sub-queries, structs, star,
  whole-row, table functions, other datasets) and red-team eval cases r1–r10.
* **Defend with.** "The LLM is an untrusted SQL author. I treat its SQL like user input to a
  database: parse it, constrain it, rewrite it. The prompt explains the rules so the model rarely
  hits them, but nothing depends on the model obeying."

### D5. k-anonymity by injected `HAVING COUNT(DISTINCT customer) >= k` plus column lineage
* **Constraint.** Age, gender, city, state, country and traffic_source are allowed only in
  aggregates; groups under k = 5 are hidden; k is configurable.
* **Alternatives.** Post-filtering result rows (too late: the aggregate is already computed and
  cannot be counted per customer); pre-aggregated views per dimension (rigid, and cannot answer
  new questions); differential privacy (strongest, but changes the numbers executives see).
* **Why.** Lineage over sqlglot scopes knows when an aggregate *touches* a demographic attribute,
  whether in keys, values or filters, and directly or via a CTE. Only those aggregates get the
  threshold. That is exact, cheap, and keeps non-demographic answers unaffected. Two classic
  bypasses are closed explicitly (finding F1): list-building aggregates and singling-out predicates.
* **Cost / known limit.** Per-query k does not stop differencing across queries. Say so
  proactively, then give the bounds:
  * the worst case is learning an attribute of a *pseudonymous* customer, because direct
    identifiers are unreachable;
  * users are authenticated internal executives with audited queries;
  * the production path is BigQuery aggregation-threshold rules, then differential privacy for
    demographic queries.
* **Why k = 5.** It is the client's number, and it matches common minimum-cell-size practice in
  statistical disclosure control. It is a config value; confirm it with their privacy officer.

### D6. Per-user product entitlements in the rewrite (production: row-access policies)
* **Constraint.** "Users see only products related to them."
* **Why.** The entitlement filter is injected into *every* table's policy sub-query: `order_items`
  by entitled product ids, `orders` and `users` through items of those products. Every metric,
  including customer demographics, is computed over the user's slice. Identity comes from
  `ToolContext`, never from model arguments, and unknown users fail closed.
* **Known nuance.** Order-level columns (`orders.num_of_item`) of an order with mixed products still
  count the other items. Item-level metrics are exact, and the instruction steers revenue to
  `order_items`.

### D7. Deletion: two-phase, token-bound, a typed phrase, deterministic execution, literal hard delete
* **Constraint.** High stakes, no recovery, "show exactly what will be deleted", and no broken UX.
* **Alternatives.** Let the model call `delete_reports` after asking "are you sure?" (the model can
  be talked into it); ADK's built-in tool confirmation (experimental, and a known issue in another
  port resumes on any matching confirmation).
* **Why.**
  * The model can only *request* a deletion. The pending action freezes the exact ids and a
    content snapshot hash, bound to user, session, a 5-minute TTL and single use.
  * The CLI, not the model, renders the list and requires `DELETE <n>`, so the user must read the
    count.
  * Deterministic code re-checks everything and deletes in a transaction.
  * "Hard delete" is literal: `secure_delete` zeroes the freed pages, FTS5 forgets the tokens, and
    audit and traces keep no content (finding F3).
* **Evidence.** Tests cover wrong phrase, replayed token, another user, another conversation
  untouched, stale snapshot, expiry, and that the raw database file no longer contains the text.

### D8. Golden trios: hybrid retrieval injected into the instruction, plus an automated promotion gate
* **Constraint.** Reuse analyst logic, keep the bucket current, and the approval flow must scale
  without a human bottleneck.
* **Alternatives.** Fine-tuning (slow to update, opaque); the trios only as a tool the model may
  call (an extra round-trip, and models skip optional tools); a semantic layer (see Q8).
* **Why.**
  * BM25 catches exact business terms ("churn", brand names); embeddings catch paraphrases. RRF
    fuses them without score calibration.
  * A relevance floor means unrelated questions get *no* examples instead of misleading ones.
  * Injecting the top 3 costs no extra round-trip.
  * Promotion is automated: guard, execution, dedup and a judge score. Humans review exceptions
    only. A nightly re-validation deprecates trios broken by schema drift.
* **Cost / known limit.** The model can over-reuse a trio's *recommendations*. Mitigated by the
  "adapt, don't copy" instruction and the numeric grounding check. The ablation measures whether
  the trios help at all (§4).

### D9. Bounded self-correction
* **Constraint.** Detect syntax errors and empty results and self-correct "without inflating costs
  or crashing".
* **Why.**
  * The guard and the BigQuery dry-run catch errors for free, before any execution cost.
  * Errors return structured hints.
  * Empty results return diagnostics: the closest real filter values, the date range, and privacy
    or entitlement notes.
  * There is a hard cap of 3 failed attempts, then `give_up` forces an explanation.
  * Infrastructure failures don't consume attempts. Budgets in $, model calls and tool calls cap
    everything else.
* **Evidence.** `test_sql_error_is_self_corrected`, `test_bounded_attempts_then_give_up`,
  `test_empty_result_returns_diagnostics`.

### D10. Observability: a custom plugin to SQLite/JSONL now, OpenTelemetry to Cloud Trace and Langfuse in production
* **Why.** "Why did this answer go wrong?" needs the exact message delta the model saw, every SQL
  attempt (original and rewritten), guard decisions and costs, all keyed by one trace id. That
  is the same data model as OpenTelemetry spans, so the production export is a sink change.
* **Data protection.** Logs are a PII sink, so traces are scrubbed at write time and never store
  report bodies (finding F2).

### D11. Persona as versioned data behind an automated quality gate
* **Constraint.** A non-developer edits it weekly, there is no redeploy, and there is no manual
  approval.
* **Why.** The persona holds tone and structure only. Safety rules are rendered *after* it in code,
  so a persona cannot weaken them. The gate (schema, policy lint, behavioural sandbox run) replaces
  the human approver. Versions are immutable, and rollback is instant.

### D12. Preferences: explicit wins; implicit preferences need 2 consistent signals
* **Why.** One "too long" should not change someone's experience permanently, but two consistent
  signals are a pattern. Explicit statements always win. This is cheap, deterministic and
  explainable (`/prefs` shows why).

### D13. Offline mode: DuckDB, sqlglot transpile, a scripted model and Ollama
* **Why.** Deterministic tests of the *real* runner and plugins need a deterministic model and a
  local warehouse. The LLM always writes BigQuery SQL, and only the adapter transpiles, so offline
  and online share the guard. Ollama shows the pipeline works with a real local model.

### D14. Storage: SQLite in the prototype, Firestore in production
* **Why Firestore.** Serverless, per-document consistency, TTL on pending actions, trivial at this
  scale (about 100 reports a day).
* **Alternative.** Cloud SQL Postgres, which has better full-text search for "reports mentioning
  Client X". Firestore needs a small search index for that. Both are defensible; I'd pick Postgres if
  report search becomes central.

---

## 2. Questions a reviewer is likely to ask

**Q1. Why let the LLM write raw SQL at all?**
Executives ask open questions that no fixed query set covers. The risk is contained structurally:
read-only, rewritten, k-protected, byte-capped and budgeted. The trios steer it toward the analysts'
metric definitions. If definitions proliferate, the next step is a semantic layer (Q8).

**Q2. What stops prompt injection hidden in the data (indirect injection)?**
Tool results are data in function responses, and the instruction says so. More importantly, the
blast radius is small by design:
- every data tool is read-only;
- the only destructive action needs a typed confirmation in the UI that the model cannot produce;
- there is no outbound tool (email or web) without the same confirmation pattern.

**Q3. How do you know the numbers in a report are not hallucinated?**
- The numeric grounding check traces every number to this turn's results or a simple derivation of
  them. It is recorded on every trace and reported in evals.
- The LLM judge scores grounding.
- Execution accuracy compares against analyst reference results.

What is not covered yet: qualitative claims ("because of a stockout"). They are framed as
hypotheses by the persona rules.

**Q4. Your golden eval looks good, but isn't the answer in the prompt?**
Partly, yes, and that is why held-out cases (own reference SQL, no matching trio) and a
`--no-golden` ablation were added. Report all three numbers. The gap between "with" and "without"
trios is the measured value of requirement 1.

**Q5. p95 latency was 38 s against a 10–30 s target.**
The tail came from the first turn in a fresh process discovering that the primary model was
overloaded (about 40 s). After the circuit breakers open, turns take 6–25 s. In production the
breaker state is shared (Memorystore) or fed by a health checker, so no user pays the discovery
cost. Reports have the 1–2 min budget.

**Q6. Why is the budget $1 if a question costs about a cent?**
It is the client's number and acts as a runaway guard (loops, giant prompts), enforced before every
model call and SQL execution. Cost accounting uses the primary model's price for all calls, which
is conservative because the fallbacks are cheaper.

**Q7. What happens with 10× the users, or 1,000 trios?**
- Cloud Run scales horizontally, and state is external.
- BigQuery cost is driven by bytes. Partitioning by `DATE(created_at)` and clustering keep scans in
  MB.
- Trios move to BigQuery Vector Search; retrieval stays one query.
- What needs work: shared breaker state, a shared result cache, and per-user rate limits.

**Q8. Why not a semantic layer (Looker / dbt metrics) instead of SQL?**
It is a strong option, and the natural next step once metric definitions must be identical across
teams. The agent would request "net_revenue by state, last 12 months" and the layer would
generate SQL. Trios are the lightweight version of that today: they carry the definitions without
a new platform. Trade-off: a semantic layer reduces SQL errors and drift but limits ad-hoc
questions to modelled metrics.

**Q9. How do you test a non-deterministic system?**
In layers:
- deterministic unit and adversarial tests;
- scenario tests through the real runner with a *scripted* model, which make exact situations
  reproducible (fallback, give-up, budget stop, deletion edge cases);
- the eval suite with thresholds, run offline on every commit and online before release;
- in production, sampled judging and metrics.

Next: run online evals N times nightly and gate on the confidence interval.

**Q10. Why not use ADK's built-in eval?**
It scores tool trajectories and response similarity. The core question here is "are the numbers
right?", which needs execution against the warehouse. The cases could also be exported to ADK's
format for trajectory checks; the two are complementary.

**Q11. Is "hard delete" really hard?**
Now, yes: see D7 and finding F3, with a test that reads the raw file. Be precise about scope:
conversation transcripts follow a separate session-retention policy (30 days), and production
backups need an expiry inside the window the client accepts. Raise this with the client
explicitly.

**Q12. Why HMAC-pseudonymise customer ids when the client didn't list ids as PII?**
Ids are the join key to PII in other systems (the CRM). Pseudonyms keep follow-ups working
("tell me more about C-3f9a12bc") while making the agent's output useless for re-identification
outside it. It costs nothing.

**Q13. What fails closed and what fails open?**
- **Closed:** unknown user (no data), heuristics, SQL guard (unresolvable SQL is rejected),
  deletion (any doubt cancels).
- **Open:** the LLM scope classifier (an outage must not block analysis, and data-layer controls
  still apply), and embeddings (fall back to BM25).

**Q14. What would you do differently with more time?**
- Engine-level privacy in BigQuery (aggregation thresholds, then differential privacy).
- A semantic layer for core metrics.
- Prompt caching of the static instruction prefix.
- Streaming answers and a web UI.
- Claim-level grounding for qualitative statements.
- Nightly repeated evals with confidence intervals.

---

## 3. Tech-lead review: what was found and fixed

A review of the first complete version found these issues. Each fix has a regression test that
fails without the fix.

| # | Finding | Severity | Fix | Test |
|---|---|---|---|---|
| F1 | **k-anonymity bypasses.** `ARRAY_AGG(STRUCT(id, city, age))` / `STRING_AGG` / `APPROX_TOP_COUNT` inside a group of ≥5 listed each member's demographics; `MAX(CASE WHEN id = 123 THEN age END)` over all users passed k but revealed one person. All 5 probes passed the guard. | High (privacy) | List-building aggregates may not carry ids or demographics. In queries that use demographics, ids may not be compared with literals. | `test_k_anonymity_bypasses_are_rejected`, `test_legitimate_lookalikes_still_pass`, red-team r9 and r10 |
| F2 | **Traces stored unredacted text.** The observability plugin runs before the redaction plugin, so model text was logged before redaction. User questions and tool arguments were logged as typed. Saved-report bodies were copied into traces. | High (privacy, deletion) | Scrub everything at the trace sink with the same redactor, and elide report bodies. | `test_traces_never_store_pii_or_saved_report_bodies` |
| F3 | **"Hard delete" was not hard.** SQLite `DELETE` leaves the bytes in free pages, and FTS5 keeps tokens. The deleted text was readable in the database file. The audit log also kept titles. | High (client requirement) | `secure_delete`, FTS5 `secure-delete`, and an audit of ids only. | `test_hard_delete_leaves_no_recoverable_text_...` (reads the raw file) |
| F4 | **Single-use token was check-then-act.** Two concurrent confirmations could both pass the check. | Medium | Atomic compare-and-set inside `BEGIN IMMEDIATE`. | same test (replay) |
| F5 | **Refused turns broke the next turn.** ADK appends a `before_run` early exit with author `model`, which the agent reads as *another agent's* message. Live, the turn after "give me their emails" re-answered the email request. | Medium (UX, visible in the demo) | Re-author the refusal as the agent in `on_event_callback`. | `test_refused_turn_stays_in_history_as_the_assistant_reply` |
| F6 | **Eval leakage.** Every golden case was a paraphrase of a trio injected into the prompt. Small integers scored by coincidence. | Medium (credibility of results) | Held-out cases with their own reference SQL, a `--no-golden` ablation, small integers ignored. | `tests/test_evals.py` |
| F7 | **`search(k=0)` returned one trio.** The ablation would have silently kept a trio in the prompt. | Low (found by the new test) | Early return for k ≤ 0. | `test_no_golden_ablation_removes_trios` |
| F8 | **No check that report numbers are real.** | Medium (R6) | Numeric grounding check on every turn, in metrics and in the eval summary. | `tests/test_grounding.py` |
| F9 | **Classifier prompt concatenated raw user text.** | Low | User text is delimited, escaped and declared to be data. | existing classifier tests |

---

## 4. Evidence to quote

* Offline: 124 tests; eval gate passed (32 cases: golden 1.00, red team 10/10, behaviour 4/4,
  resilience 3/3, 0 PII leaks, numeric grounding 0.99).
* Online (2026-11-16, Gemini free tier and BigQuery): gate passed (golden 0.918, judge 5.0, red team
  8/8, behaviour 4/4, resilience 3/3, p95 38.3 s, $0.09 for 26 cases, 0 PII leaks).
* Online held-out and ablation: see [EVALUATION.md §4](EVALUATION.md#4-results).

---

## 5. Roadmap (prioritised)

| Priority | Item | Why | Effort |
|---|---|---|---|
| P0 before real users | BigQuery authorized views, row-access policies per entitlement group, and aggregation-threshold rules; agent service account without base-table access | Engine-level enforcement under the guard (defence in depth) | days |
| P0 | IAP / IdP identity mapped to entitlements; Secret Manager for keys and the HMAC secret | Trusted identity | days |
| P0 | Shared breaker state and result cache (Memorystore); per-user rate limits | Multi-instance correctness, latency tail, abuse | days |
| P0 | Retention: spans 30 days, sessions 30 days, backups with expiry | Makes "hard delete" true end to end | hours |
| P1 | Nightly online evals ×N with confidence intervals; thresholds re-set from data | Trustworthy release gate | days |
| P1 | Prompt caching of the static instruction prefix; ADK event compaction for long sessions | Latency and cost; long conversations | days |
| P1 | Route report intents to a Pro model; stream answers | Report quality; perceived latency | days |
| P1 | Semantic layer for core metrics (net revenue, churn, AOV) | Consistent definitions, fewer SQL errors | weeks |
| P2 | Differential privacy for demographic queries | Formal defence against differencing | weeks |
| P2 | Claim-level grounding (judge checks each qualitative claim against tool outputs) | Catch narrative hallucinations | days |
| P2 | Email and web-trends tools (confirmation pattern; isolated web sub-agent) | Requested extensibility | days each |
| P2 | Admin UI for personas and trios; trio quality weights learned from feedback | Non-developer ergonomics; retrieval quality | weeks |

---

## 6. A five-minute walkthrough for the review meeting

1. **Problem and constraints** (30 s). Executives, natural language, PII never reaches the LLM,
   k = 5, hard delete, $1/question, 10–30 s.
2. **Architecture** (60 s). One ADK agent; two plugins as the policy choke point; a governed SQL
   path (guard, dry-run, cap, scan, pseudonymise); golden retrieval in the instruction. Show the
   Mermaid diagram.
3. **The safety story** (60 s). "The model is an untrusted SQL author." Show a rewritten query, then
   a blocked `ARRAY_AGG` attack.
4. **The deletion story** (45 s). Live: "delete all reports from this conversation", the exact list,
   `DELETE 2`, and that the model cannot confirm.
5. **Resilience and observability** (45 s). `--llm-chaos 1.0` falls back; `retail-agent trace` shows
   the full correspondence.
6. **Quality** (45 s). Eval gate numbers, the held-out and ablation numbers, the grounding check, and
   the known limits with their mitigations.
7. **Roadmap** (15 s). The P0 list above.
