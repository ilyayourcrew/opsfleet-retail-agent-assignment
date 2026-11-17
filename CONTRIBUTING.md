# Contributing: how this codebase is changed safely

The agent is non-deterministic. Its safety must not be. These rules keep both true as the team grows.

## Change classes

| Class | Examples | What must pass before merge | Reviewers |
|---|---|---|---|
| **Safety-critical** | `safety/sql_guard.py`, `safety/pii.py`, `config/policy.yaml`, `agent/plugins.py`, `reports/store.py` (deletion), `config/entitlements.yaml` | an adversarial test for the new or changed behaviour (it must fail without the change), full `pytest`, offline eval gate | 2, one of them the safety owner |
| **Behavioural** | `agent/instructions.py`, tool docstrings, model ids, retrieval | `pytest`, offline eval gate, **online eval gate** (`workflow_dispatch` with `online: true`) | 1 |
| **Content** (no code) | personas, golden trios | their own automated gates (`persona publish`, `learn`); no PR needed | none (gated) |
| **Dependency** | version upgrades, one PR each | CI; for `google-adk` and `sqlglot` also the online eval gate (the safety logic depends on their internals) | 1 |
| Everything else | CLI, docs, scripts | CI | 1 |

## Definition of done
* Behaviour is covered by a test, and by an eval case if it depends on the model.
* Every production incident or user-reported bad answer becomes a test or an eval case before the
  fix is merged. The eval set grows from real failures.
* A changed requirement mapping is reflected in `docs/REQUIREMENTS_TRACEABILITY.md`.
* A design decision that changed is updated in `docs/DESIGN_DECISIONS.md`: what changed, and why.

## Adding a capability (a tool)
1. Write a typed function with a clear docstring in `tools/` and register it with `@register(...)`.
   Tracing, budgets, PII scanning of outputs, error containment and identity (`ToolContext`) apply
   automatically through the plugins.
2. Read identity only from `ToolContext`, never from model arguments.
3. Outward-facing or destructive actions (sending e-mail, deleting) follow the two-phase pattern of
   `request_report_deletion`: the tool *requests*, the interface confirms, deterministic code
   executes.
4. Add a scenario test with `ScriptedLlm` and a behaviour eval case.

## Local checks (the same as CI)
```bash
ruff check src tests evals scripts
pytest -q
python evals/run_evals.py
```
