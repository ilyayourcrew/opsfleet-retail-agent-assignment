# Assumptions and client answers

## Answers received from OpsFleet (Slack, Harry and Roi)

| # | Question | Answer (design input) |
|---|---|---|
| Q1 | PII classification and compliance | No regime (GDPR/CCPA/residency), but PII must never reach the LLM or appear in output. Never shown: first and last name, email, street address, postal code, exact coordinates. Age, gender, city, state, country and traffic source only in aggregates. Hide demographic groups below a minimum size (default **5**, configurable, documented). Masking must be more robust than column-name matching. IP address appears only in `events`: list it as PII in the design. No Sensitive Data Protection (DLP) or policy tags in use. |
| Q2 | Golden Knowledge | Theoretical: define the format and seed a sample. Explain updates over time and query-time retrieval. **The approval flow should scale** (human review doesn't). |
| Q3 | Scale / latency / cost | Tens to a few hundred executives, few concurrent. Sizing: ~100 active managers/day × ~10 questions, ~1 saved report each, ~1M new rows/day. Simple questions 10–30 s, long reports 1–2 min. Budget **$1 per question**, configurable (design). |
| Q4 | Destructive ops | **Hard delete**, no recovery window. Reports are private to their author: only the author can see/modify/delete; no manager/admin delete or restore. Strict confirmation flow, e.g. showing the exact list before deleting. |
| Q5 | Persona | A few non-developers (assume one) change it, without code edits or redeploy. Changes pass an **automated quality gate** before going live, no manual approval. |

## My assumptions for open points

| Topic | Assumption | Where it matters |
|---|---|---|
| Trio curation owner | The analytics team lead owns the golden bucket. Automated gates do first-pass review; humans review exceptions in a weekly digest and can veto. | §4.1 |
| Persona per audience | One global persona by default; the design supports optional per-role overrides (CEO vs category leads) keyed on the entitlement role. | §4.8 |
| "Products related to him" | Entitlements by product department / category / brand (AND across dimensions, OR within). A restricted user sees only order items of entitled products, orders containing them and customers who bought them. The CEO is unrestricted. Unknown users get **no** data (fail closed). | §4.2 |
| Identity | Prototype: `--user` flag mapped to `config/entitlements.yaml`. Production: IAP + IdP groups. | §6 |
| Customer identifiers | `users.id` / `user_id` are not PII by the client's list but are pseudonymised (keyed HMAC) in outputs to prevent linkage; allowed for grouping/joining. | §4.2 |
| `orders.gender` | Treated like `users.gender`: aggregate-only. | `config/policy.yaml` |
| Revenue definition | Net revenue = `SUM(order_items.sale_price)` excluding `Cancelled` and `Returned` items (encoded in the golden trios, which steer the agent). | trios |
| Time | All timestamps UTC; "last month" = previous complete calendar month; Q1 = Jan–Mar of the current year unless stated. | trios |
| Report storage | A saved report is markdown + metadata (title, session, SQL used). It is scanned for PII before saving. | §4.3 |
| Deletion scope "this conversation" | = reports whose `session_id` equals the current chat session. | §4.3 |
| Confirmation phrase | Typing `DELETE <n>` (n = number of reports listed) is strict enough to prevent accidents, and is fast. | §4.3 |
| Offline mode | A synthetic DuckDB copy with the same schema (including *fake* PII to exercise masking) is acceptable for development and CI; real numbers come from BigQuery. | README |
| Model availability | Gemini model ids change frequently; they are configuration and were verified against the live model list at test time (see EVALUATION.md). | §2 |
