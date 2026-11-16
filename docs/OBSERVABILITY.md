# Observability (requirement 7)

Goal: know **when** the agent is failing (metrics and alerts) and **why** (a trace of everything the
model saw and did in that turn).

## 1. What is recorded

Every user turn is one **trace** (`trace_id` = ADK invocation id), written by `ObservabilityPlugin`
(`src/retail_agent/agent/plugins.py`) into `turns` and `spans` (SQLite in the prototype,
`var/app.sqlite`, plus `var/traces.jsonl`; OpenTelemetry → Cloud Trace / Langfuse and a BigQuery log
sink in production).

| Span | Attributes |
|---|---|
| `guard/input_guard` | verdict, category, reason, source (heuristic / llm) |
| `event/implicit_preference_signal` | signals learned from the message |
| `model/request` | model, messages in context, **delta**: the exact new messages (user text, function responses) sent on this call |
| `model/response` | model version (shows fallback), tokens in/out/**cached**, latency, finish reason, parts (text, function calls with args) |
| `tool/run_analysis_sql` | attempt #, purpose, **original SQL**, **rewritten SQL**, error type + message, rows, bytes, warehouse ms, cache hit, PII masked, **BigQuery job id** (join to `INFORMATION_SCHEMA.JOBS` for slot time, bytes, cancellation) |
| `tool/<other>` | args, status, result preview, latency |
| `guard/*` | budget exceeded, attempts exhausted, output / tool-output PII redacted (with finding types) |
| `error/*` | model or tool exception type and message |
| `event/deletion_requested`, `event/chart_created` | ids, paths |
| `event/numeric_grounding` | share of numbers in the answer traced to this turn's results, the ungrounded ones (status `warn` below 0.8) |

The **turn row** aggregates: user, session, question, answer preview, status
(`ok | refused | error | degraded | budget_exceeded | timeout`), refusal category, latency, model calls,
tool calls, SQL attempts / failures / empties / self-corrections, guard blocks, PII redactions, tokens,
LLM $ and BigQuery $, fallback used, degraded, persona version, golden trio ids, provider.
Feedback (`/feedback + | - comment`) is joined by trace id.

Privacy of telemetry: result sets are PII-free by construction, and the trace store additionally
**scrubs everything at write time** (questions users type, model text, tool arguments, previews) with
the same value-shape redactor, because logs are where PII leaks are least noticed. Saved-report bodies
are never logged (only their length), so the hard delete of a report is not undone by its traces.
Retention: 30 days for spans in production (Cloud Logging bucket retention), turn-level metrics kept
longer.

## 2. Agent-level metrics

`retail-agent stats [--hours N]` (`src/retail_agent/observability/metrics.py`). Production computes the
same definitions as scheduled queries over the log sink, feeding Cloud Monitoring.

| Metric | Definition | Why | Alert (prod) |
|---|---|---|---|
| Success rate | turns with status `ok` / turns | headline health | < 90% over 1 h |
| Error rate | status `error` or `timeout` / turns | crashes, bugs | > 2% over 15 min, **page** |
| Refusal rate by category | refused / turns, per category | attacks vs false positives on legitimate questions | spike > 3× baseline |
| Latency p50 / p95 | turn wall time | SLO 10–30 s simple, 1–2 min reports | p95 > 45 s (simple) |
| Model calls per turn | mean | loops, prompt regressions | > 6 |
| SQL failure rate | failed attempts / attempts | text-to-SQL quality, schema drift | > 25% |
| Empty-result rate | empty / attempts | wrong filters, data freshness | > 20% |
| Self-correction success | turns that recovered after a failure | value of the correction loop | trend |
| Turns needing retry / give-ups | turns with > 1 attempt / with `give_up` | difficult question types | trend |
| Guard blocks | policy violations by the model's SQL, input blocks | attacks, prompt regressions | spike |
| PII redactions | redactions in tool results and answers | **should be ~0 in final answers** | any in final answer: **page** |
| Fallback-model turns | responses served by the fallback | provider health | > 10% over 15 min |
| Degraded turns | all models down | outage | any, **page** |
| Budget-exceeded turns | stopped by $ / call caps | cost runaways, loops | > 1% |
| Cost per turn, total | LLM tokens × price + BigQuery bytes × price | budget ($1/question) | daily total > 2× forecast |
| Tokens in/out | sums | prompt bloat | trend |
| Prompt-cache hit share | cached input tokens / input tokens | cost and latency of the static instruction prefix | a drop after a prompt change means the prefix broke |
| Numeric grounding | mean share of answer numbers traced to query results; turns below 0.8 | hallucinated figures in answers and reports | mean < 0.9 daily; low turns go to the review queue |
| Feedback positive rate | 👍 / rated turns | perceived quality | < 70% weekly |
| Golden trio hit rate | turns with ≥ 1 retrieved trio | knowledge coverage gaps | trend |

## 3. Deep-dive debugging workflow

1. **Find the turn**: from an alert, a user report ("reference: 1a2b3c4d" is shown with every error and
   in every answer footer) or `stats` → `retail-agent trace <id-or-prefix>` (or `/trace` in chat for the
   last turn).
2. **Read the header**: status, error, counters, persona version, golden trios used, provider/model.
   Typical diagnoses:
   * `sql_failures` high, `golden=[]`: knowledge gap; add a trio or fix retrieval synonyms.
   * `guard_blocks` > 0: the model tried something disallowed (PII, row-level demographics). Check whether
     the user asked for it (attack) or the prompt misled it.
   * `fallback=True` or `status=degraded`: provider incident; check the model error span.
   * `budget_exceeded`: loop. Inspect the repeated tool calls.
3. **Read the correspondence**: each `model/request` span shows exactly what was added to the model's
   context (user message, tool results with status and hints), each `model/response` what it decided
   (text or function call with full SQL). `run_analysis_sql` spans show the original vs rewritten SQL,
   the error the model received and the next attempt, so you can see whether the hint helped.
4. **Reproduce**: copy the SQL into the guard (`SqlGuard.check`) or BigQuery; replay the question offline
   with `retail-agent ask "<question>" --trace`; turn the failure into an eval case in
   `evals/cases.yaml` so it stays fixed.

In production the same spans appear in Langfuse (LLM view with token and cost per span) and Cloud Trace
(latency waterfall across agent-api, Vertex AI and BigQuery), linked by `trace_id`; logs carry the
same id.

## 4. Example

```
$ retail-agent trace 1a2b3c4d
Trace e-1a2b3c4d-...  user=ceo  status=ok  latency=14.2s  cost=$0.01021
model_calls=4 tool_calls=3 sql_attempts=3 sql_failures=1 empty=0 self_corrected=1 guard_blocks=1 ...
persona=executive_crisp@default  golden=['state_spend_comparison', ...]
Q: Why are users in Texas underspending vs California?
 #  kind   name               status  details
 0  guard  input_guard        ok      {"category": "analysis", "source": "llm"}
 1  model  request                    user: "Why are users in Texas underspending vs California?"
 2  model  response                   gemini-... in=7012 out=310 [function_call run_analysis_sql {...}]
 3  tool   run_analysis_sql   error   attempt 1 ... policy_violation: Demographic attributes (city) can only be shown in aggregates
 4  model  request                    user: [function_response run_analysis_sql status=error ...hint...]
 ...
```

See [EXAMPLE_RUN.md](EXAMPLE_RUN.md) for real traces.
