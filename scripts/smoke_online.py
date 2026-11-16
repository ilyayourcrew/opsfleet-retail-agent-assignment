"""Online smoke test: real BigQuery + (optionally) Gemini.

    python scripts/smoke_online.py            # BigQuery checks (+ Gemini checks if GOOGLE_API_KEY is set)
    python scripts/smoke_online.py --no-llm   # BigQuery only

1. Schema drift: live table schemas vs the catalog in warehouse/schema.py.
2. Every golden trio: guard -> dry-run (bytes) -> execute (rows) on the real dataset.
3. Gemini: list available models, ping primary + fallback, embeddings.
Exit code 1 on any failure.
"""

from __future__ import annotations

import argparse
import sys
import time

from retail_agent.config import get_settings
from retail_agent.golden.store import TrioStore
from retail_agent.safety.policy import PolicyStore
from retail_agent.safety.sql_guard import SqlGuard
from retail_agent.warehouse.factory import build_warehouse
from retail_agent.warehouse.schema import TABLES


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    s = get_settings().model_copy(update={"warehouse": "bigquery"})
    ok = True

    print("== BigQuery ==")
    wh = build_warehouse(s)
    runner = wh.inner.runner
    print(f"project={runner.client.project} dataset={s.bq_dataset}")
    for name, table in TABLES.items():
        live = {c["name"]: c["type"] for c in runner.get_table_schema(name)}
        ours = {c.name: c.type for c in table.columns}
        norm = {"INTEGER": "INT64", "FLOAT": "FLOAT64"}
        live = {k: norm.get(v, v) for k, v in live.items()}
        missing, extra = set(ours) - set(live), set(live) - set(ours)
        mismatched = {k for k in set(ours) & set(live) if ours[k] != live[k]}
        status = "OK" if not (missing or extra or mismatched) else "DRIFT"
        ok &= status == "OK"
        print(f"  schema {name:<12} {status} missing={sorted(missing)} extra={sorted(extra)} type_mismatch={sorted(mismatched)}")

    policies = PolicyStore(s.config_dir)
    guard = SqlGuard(policies.policy(), wh.physical_table)
    ceo = policies.entitlement("ceo")
    total_bytes = 0
    for trio in TrioStore(s.golden_dir).active():
        try:
            checked = guard.check(trio.sql, ceo)
            d = wh.dry_run(checked.sql)
            t0 = time.time()
            res = wh.execute(checked.sql, 50)
            total_bytes += res.bytes_billed
            print(f"  trio {trio.id:<28} OK rows={len(res.df):<4} scanned={d.bytes_processed / 1e6:7.1f} MB  {time.time() - t0:4.1f}s")
            if res.df.empty:
                print("     WARNING: empty result on live data")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  trio {trio.id:<28} FAIL {getattr(e, 'message', e)}")
    print(f"  total billed: {total_bytes / 1e6:.1f} MB (~${total_bytes / 2**40 * s.bq_price_per_tib:.4f})")

    if not args.no_llm:
        print("== Gemini ==")
        if not s.google_api_key:
            print("  GOOGLE_API_KEY not set - skipped")
        else:
            from google import genai

            from retail_agent.llm.factory import _export_key

            _export_key(s)
            client = genai.Client()
            names = sorted(m.name.removeprefix("models/") for m in client.models.list())
            flash = [n for n in names if "flash" in n and "gemini" in n]
            print(f"  available flash models: {', '.join(flash[:25])}")
            for m in dict.fromkeys([s.model_primary, *s.fallback_models, s.model_light]):
                try:
                    t0 = time.time()
                    r = client.models.generate_content(model=m, contents="Reply with the single word: ok")
                    print(f"  {m:<28} OK {r.text.strip()[:10]!r} {time.time() - t0:.1f}s")
                except Exception as e:  # noqa: BLE001
                    ok = False
                    print(f"  {m:<28} FAIL {e}")
            try:
                r = client.models.embed_content(model=s.embedding_model, contents=["monthly revenue"])
                print(f"  {s.embedding_model:<28} OK dim={len(r.embeddings[0].values)}")
            except Exception as e:  # noqa: BLE001
                print(f"  {s.embedding_model:<28} WARN {e} (retrieval falls back to BM25)")
    print("RESULT:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
