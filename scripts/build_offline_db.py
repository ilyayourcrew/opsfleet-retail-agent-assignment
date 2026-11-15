"""Build the offline DuckDB copy of thelook_ecommerce (synthetic data, identical schema).

Usage:  python scripts/build_offline_db.py [--users 15000] [--products 1200] [--seed 7]
"""

from __future__ import annotations

import argparse
import time

from retail_agent.config import get_settings
from retail_agent.warehouse.synthetic import generate, write_duckdb


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--users", type=int, default=15000)
    ap.add_argument("--products", type=int, default=1200)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    path = get_settings().duckdb_path
    t0 = time.time()
    frames = generate(n_users=args.users, n_products=args.products, seed=args.seed)
    write_duckdb(frames, path)
    sizes = ", ".join(f"{k}={len(v):,}" for k, v in frames.items())
    print(f"Offline warehouse written to {path} ({sizes}) in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
