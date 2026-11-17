# Architecture (HLD)

This document is the high-level design: building blocks, services, compute, data stores and the
flows between them. The *why* behind each choice and the per-requirement details are in
[TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md).

The prototype in this repository implements the same building blocks with local stand-ins
(table at the end), so every box below has running code behind it.

## 1. Production architecture on Google Cloud

```mermaid
flowchart LR
    subgraph Clients["Clients"]
        CLI["CLI (prototype)"]
        WEB["Chat UI / Slack app (prod)"]
    end

    subgraph Edge["Edge & identity"]
        IAP["Identity-Aware Proxy + Workspace IdP<br/>(user identity, groups)"]
    end

    subgraph Compute["Cloud Run: agent-api (stateless, autoscaled)"]
        RUN["ADK Runner"]
        subgraph Plugins["ADK plugins (run on every turn)"]
            IG["Input guard"]
            OG["PII output guard"]
            BG["Budget / cost guard"]
            OBS["Tracing + metrics"]
        end
        AG["Analyst LlmAgent<br/>(instruction = persona + user prefs + policy)"]
        subgraph Tools["Tool registry (extension point)"]
            T1["describe_schema"]
            T2["search_golden_knowledge"]
            T3["run_analysis_sql"]
            T4["save / list reports"]
            T5["request_report_deletion"]
            T6["remember_preference"]
            T7["create_chart"]
            T8["(email_report, web_trends ...)"]
        end
        SG["SQL guard<br/>(sqlglot AST: read-only, allowlist,<br/>PII-free views, entitlements, k-anonymity, LIMIT)"]
        DEL["Deletion service<br/>(two-phase, token-bound confirm)"]
    end

    subgraph Models["Vertex AI"]
        GEM["Gemini Flash (primary)"]
        GEML["Gemini Flash-Lite (fallback + guard classifier)"]
        EMB["Gemini embeddings"]
    end

    subgraph Data["Data plane"]
        BQ[("BigQuery<br/>thelook_ecommerce<br/>(read-only SA, authorized views,<br/>row-access policies, bytes cap)")]
        GCS[("GCS golden bucket<br/>trios: question → SQL → report")]
        VS[("BigQuery Vector Search<br/>trio embeddings index")]
        FS[("Firestore<br/>reports, preferences, personas,<br/>pending actions, sessions")]
    end

    subgraph Ops["Async jobs & operations"]
        PS["Pub/Sub: turn-completed, feedback"]
        JOB1["Cloud Run Job: trio miner + promotion gate"]
        JOB2["Cloud Run Job: persona quality gate / eval suite"]
        SCH["Cloud Scheduler: nightly re-validation of trios"]
        LOG["Cloud Logging + Cloud Trace (OpenTelemetry)"]
        LF["Langfuse (LLM traces, evals)"]
        MON["Cloud Monitoring dashboards + alerts"]
        SM["Secret Manager"]
    end

    CLI --> IAP
    WEB --> IAP
    IAP --> RUN
    RUN --> IG --> AG
    AG <--> GEM
    GEM -. 429/5xx .-> GEML
    AG --> Tools
    T3 --> SG --> BQ
    T2 --> VS
    VS --- GCS
    T4 --> FS
    T5 --> DEL --> FS
    T6 --> FS
    AG --> OG --> RUN
    OBS --> LOG
    OBS --> LF
    LOG --> MON
    RUN --> PS
    PS --> JOB1
    JOB1 --> GCS
    JOB1 --> EMB --> VS
    SCH --> JOB1
    JOB2 --> FS
    RUN --> SM
```

### Compute & communication

| Component | Runs on | Talks to | Protocol |
|---|---|---|---|
| Chat front (CLI / web / Slack) | user device / Cloud Run | agent-api | HTTPS (SSE streaming), IAP-authenticated |
| agent-api (ADK Runner, plugins, tools) | Cloud Run, min 1 instance, concurrency 20 | Vertex AI, BigQuery, Firestore, Pub/Sub | gRPC / REST client libraries, service account per env |
| Gemini models | Vertex AI (regional endpoint) | - | REST, per-request timeout, retry on 429/5xx, model fallback |
| SQL execution | BigQuery (on-demand) | - | Jobs API: dry-run first, then query with `maximum_bytes_billed` |
| Golden retrieval | BigQuery Vector Search over GCS-ingested trios | - | SQL `VECTOR_SEARCH` + BM25 keyword fallback |
| Async learning / gates | Cloud Run Jobs fed by Pub/Sub + Scheduler | GCS, BigQuery, Firestore | push subscriptions |
| Telemetry | OpenTelemetry SDK in agent-api | Cloud Trace / Logging, Langfuse | OTLP |

### Where data lives

| Data | Store | Retention / notes |
|---|---|---|
| Raw transactions | BigQuery `thelook_ecommerce` (read-only) | never copied; the agent sees only policy views |
| Golden trios | GCS `gs://<env>-golden-knowledge/trios/*.yaml` (+ embeddings in BigQuery) | versioned objects; status `candidate` / `active` / `deprecated` |
| Conversation sessions | Firestore (ADK session service) | 90 days |
| Saved reports | Firestore `reports/{id}` (owner-scoped) + search index | hard delete on request, no recovery (client requirement) |
| User preferences | Firestore `users/{id}/preferences` | until user resets |
| Personas | Firestore `personas/{name}/versions/{n}` + `active` pointer | full history for rollback |
| Pending destructive actions | Firestore (TTL 5 min) | single use |
| Traces & metrics | Cloud Logging / Trace, BigQuery log sink, Langfuse | 30 days hot, 1 year in BigQuery |
| Secrets (API keys, pseudonym HMAC key) | Secret Manager | rotated |

**PII never leaves BigQuery**: the SQL guard rewrites every query onto policy views that do not
contain PII columns, so PII is never in a result set, never in the LLM context, never in logs.

## 2. Request flow: an analysis question

```mermaid
sequenceDiagram
    autonumber
    actor U as Executive
    participant CLI as CLI
    participant R as ADK Runner + plugins
    participant A as Analyst agent (Gemini)
    participant K as Golden knowledge
    participant G as SQL guard
    participant W as BigQuery

    U->>CLI: "Why are users in Texas underspending vs California?"
    CLI->>R: run_async(user, session, message)
    R->>R: Input guard (heuristics + Flash-Lite classifier)
    alt malicious / off-topic
        R-->>CLI: polite refusal (no model/tool call wasted)
    end
    R->>K: retrieve top-k trios for the question (hybrid BM25 + embeddings)
    R->>A: instruction = persona + user prefs + policy + retrieved trios
    loop until answer, max attempts / budget
        A->>G: run_analysis_sql(sql, purpose)
        G->>G: parse, read-only, allowlist, rewrite to PII-free entitled views, k-anonymity, LIMIT
        G->>W: dry-run (syntax + bytes) then execute
        alt syntax error / policy violation / empty result
            G-->>A: structured error + hint + attempts_left
        else rows
            W-->>G: rows
            G->>G: PII value scan, pseudonymise customer ids
            G-->>A: sample rows + stats
        end
    end
    A-->>R: answer (markdown, user-preferred format)
    R->>R: PII output guard, cost meter, trace span close
    R-->>CLI: answer + trace id + cost
```

## 3. Destructive operation flow: deleting reports

```mermaid
sequenceDiagram
    autonumber
    actor U as Executive
    participant CLI as CLI
    participant A as Analyst agent
    participant D as Deletion service
    participant S as Report store

    U->>CLI: "Delete all the reports we made in this conversation"
    CLI->>A: message
    A->>D: request_report_deletion(scope="this_conversation")
    D->>S: SELECT ... WHERE owner_id = user AND session_id = current
    D->>D: create pending action {ids snapshot, hash, user, session, ttl=5m, single-use}
    D-->>A: {status: awaiting_confirmation, reports: [...]}
    A-->>CLI: "I found 3 reports ... please confirm"
    CLI->>U: exact list (id, title, created) + "type DELETE 3 to confirm"
    U->>CLI: DELETE 3
    CLI->>D: confirm(token, user, session, typed phrase) - deterministic code, LLM not involved
    D->>S: re-check ownership + snapshot unchanged, hard DELETE
    D-->>CLI: deleted 3
    S-->>A: next turn - audit log in the instructions: "reports #1-#3 DELETED (confirmed by the user)"
```

## 4. Prototype mapping

| Production building block | Prototype stand-in (this repo) |
|---|---|
| Cloud Run agent-api | local Python process (`retail-agent chat`) running the same ADK Runner |
| Vertex AI Gemini | Gemini API (AI Studio key) via ADK `Gemini` + `FallbackModel`; offline: Ollama via `LiteLlm` or a deterministic scripted model |
| BigQuery | BigQuery (`bigquery-public-data.thelook_ecommerce`); offline: DuckDB synthetic copy with the same schema (SQL transpiled by sqlglot) |
| GCS golden bucket + Vector Search | `data/golden/trios/*.yaml` + BM25 (+ Gemini embeddings when online), RRF fusion |
| Firestore | SQLite `var/app.sqlite` (reports + FTS5, preferences, personas, pending actions, traces) and `var/sessions.sqlite` (ADK sessions) |
| Cloud Trace / Logging / Langfuse | trace plugin writing spans to SQLite + `var/traces.jsonl`; `/trace` and `retail-agent stats` |
| Cloud Run Jobs (gates) | `retail-agent persona publish`, `retail-agent learn`, `python evals/run_evals.py` |
| IAP identity | `--user` flag mapped to `config/entitlements.yaml` |
