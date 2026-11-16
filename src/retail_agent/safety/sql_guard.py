"""SQL guard: the enforcement point between LLM-written SQL and the warehouse.

Safety is enforced *structurally*, not by prompt:

1. Parse (BigQuery dialect). Exactly one statement, and it must be a read-only query.
2. Deny DML/DDL/scripting nodes anywhere in the tree and exfiltration-capable functions.
3. Table allowlist: only orders, order_items, products, users (bare or dataset-qualified).
   CTEs may not shadow those names. INFORMATION_SCHEMA / other datasets / wildcards denied.
4. PII columns are rejected by name with a clear message (fast feedback for self-correction) and
   - more importantly - the query is *qualified against the policy-view schema*, which does not
   contain PII columns at all. `SELECT *`, `u.*`, `TO_JSON_STRING(u)` therefore cannot reach PII.
5. Mini column lineage over sqlglot scopes:
   * quasi-identifiers (age, gender, city, state, country, traffic_source) may not appear as raw
     values in the final rows;
   * any aggregation that touches quasi-identifiers (in keys, values or filters, directly or via a
     sub-query/CTE) gets `HAVING COUNT(DISTINCT <customer id>) >= k` injected - small groups are
     suppressed (k-anonymity, k=5 default);
   * customer-id output columns are reported so results can be pseudonymised;
   * list-building aggregates (ARRAY_AGG, STRING_AGG, APPROX_TOP_*, APPROX_QUANTILES) may not carry
     customer ids or quasi-identifiers - they would list each group member's value;
   * in queries that use quasi-identifiers, customer ids may not be compared with literals
     (singling out: MAX(CASE WHEN id = 123 THEN age END) passes k but reveals one person).
6. Every base table is replaced by a policy sub-query: PII-free projection + the user's product
   entitlement filter (order_items/orders/users are restricted to entitled products too).
7. A LIMIT is enforced on the final result.

The output is BigQuery SQL that the warehouse adapter runs (DuckDB transpiles it).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from retail_agent.safety.policy import DataPolicy, Entitlement

DATASET_NAMES = {"thelook_ecommerce"}
PROJECT_NAMES = {"bigquery-public-data"}

_DENIED_NODES: tuple[type[exp.Expression], ...] = tuple(
    t
    for t in (
        getattr(exp, n, None)
        for n in (
            "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command",
            "TruncateTable", "Grant", "Revoke", "Set", "Use", "Transaction", "Commit", "Rollback",
            "Copy", "LoadData", "Export", "Into", "Declare", "Execute", "Analyze", "Pragma",
        )
    )
    if t is not None
)

# Aggregates that return *collections of input values* instead of a summary. Over a k-anonymous
# group they would still list each member's value (ARRAY_AGG(STRUCT(id, city, age))), so customer
# ids and demographic attributes may not appear inside them.
_LISTING_AGGS: tuple[type[exp.Expression], ...] = tuple(
    t
    for t in (
        getattr(exp, n, None)
        for n in ("ArrayAgg", "GroupConcat", "ArrayConcatAgg", "ArrayUniqueAgg", "ApproxTopK", "ApproxTopSum",
                  "ApproxQuantiles")
    )
    if t is not None
)
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between)


class GuardViolation(Exception):
    """Raised when SQL is rejected. `kind` is 'syntax' or 'policy'; `hint` helps the LLM fix it."""

    def __init__(self, kind: str, message: str, hint: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint


@dataclass
class GuardResult:
    sql: str  # rewritten, policy-enforced BigQuery SQL
    original_sql: str
    tables: list[str]
    k_enforced_scopes: int
    pseudonymize_columns: list[str]
    limit_applied: int
    notes: list[str] = field(default_factory=list)


@dataclass
class _ScopeInfo:
    raw_quasi: set[str] = field(default_factory=set)  # output cols carrying raw quasi values
    uid: set[str] = field(default_factory=set)  # output cols carrying raw customer ids
    tainted: bool = False  # rows depend on quasi-identifier filters/joins not yet k-protected


class SqlGuard:
    def __init__(
        self,
        policy: DataPolicy,
        physical_table: Callable[[str], str],
        max_rows: int = 200,
    ) -> None:
        self.policy = policy
        self.physical_table = physical_table
        self.max_rows = max_rows

    # ------------------------------------------------------------------ public API
    def check(self, sql: str, entitlement: Entitlement) -> GuardResult:
        root = self._parse(sql)
        self._deny_dangerous(root)
        cte_names = self._cte_names(root)
        tables = self._check_tables(root, cte_names)
        self._check_pii_names(root)
        root = self._qualify(root)
        k_scopes, uid_cols, notes = self._lineage_and_k(root)
        self._rewrite_tables(root, cte_names, entitlement)
        limit = self._enforce_limit(root)
        return GuardResult(
            sql=root.sql(dialect="bigquery"),
            original_sql=sql,
            tables=sorted(tables),
            k_enforced_scopes=k_scopes,
            pseudonymize_columns=sorted(uid_cols),
            limit_applied=limit,
            notes=notes,
        )

    # ------------------------------------------------------------------ steps
    def _parse(self, sql: str) -> exp.Expression:
        text = (sql or "").strip().rstrip(";").strip()
        if not text:
            raise GuardViolation("syntax", "Empty SQL.")
        try:
            statements = [s for s in sqlglot.parse(text, read="bigquery") if s is not None]
        except ParseError as e:
            first = e.errors[0] if e.errors else {}
            desc = first.get("description", str(e)) if isinstance(first, dict) else str(e)
            raise GuardViolation("syntax", f"Syntax error: {desc}", "Check commas, parentheses and quoting.") from e
        if len(statements) != 1:
            raise GuardViolation("policy", "Exactly one SQL statement is allowed.", "Send a single SELECT query.")
        root = statements[0]
        if not isinstance(root, exp.Query):
            raise GuardViolation("policy", "Only read-only SELECT queries are allowed.", "Rewrite as a SELECT.")
        return root

    def _deny_dangerous(self, root: exp.Expression) -> None:
        bad = root.find(*_DENIED_NODES) if _DENIED_NODES else None
        if bad is not None:
            raise GuardViolation("policy", f"Statement type {bad.key.upper()} is not allowed (read-only access).")
        for fn in root.find_all(exp.Func):
            name = (fn.sql_name() if not isinstance(fn, exp.Anonymous) else str(fn.this)).upper()
            if name in self.policy.denied_functions or any(name.startswith(d.rstrip("*")) for d in self.policy.denied_functions if d.endswith("*")):
                raise GuardViolation("policy", f"Function {name} is not allowed.")

    @staticmethod
    def _cte_names(root: exp.Expression) -> set[str]:
        return {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}

    def _check_tables(self, root: exp.Expression, cte_names: set[str]) -> set[str]:
        allowed = set(self.policy.allowed_tables)
        if cte_names & allowed:
            raise GuardViolation(
                "policy",
                f"CTE names may not shadow source tables: {sorted(cte_names & allowed)}",
                "Rename the CTE, e.g. users_agg.",
            )
        used: set[str] = set()
        for t in root.find_all(exp.Table):
            name = (t.name or "").lower()
            db = (t.db or "").lower()
            catalog = (t.catalog or "").lower()
            if not name or isinstance(t.this, exp.Func):
                raise GuardViolation("policy", "Table functions are not allowed.")
            if not db and name in cte_names:
                continue
            if "information_schema" in (name, db) or "*" in name:
                raise GuardViolation("policy", "Metadata/wildcard tables are not allowed.", "Use describe_schema instead.")
            if name not in allowed or (db and db not in DATASET_NAMES) or (catalog and catalog not in PROJECT_NAMES):
                raise GuardViolation(
                    "policy",
                    f"Table '{t.sql(dialect='bigquery')}' is not available.",
                    f"Only these tables can be queried: {', '.join(sorted(allowed))}.",
                )
            # normalise to the bare logical name; physical name is injected at rewrite time
            t.set("catalog", None)
            t.set("db", None)
            used.add(name)
        if not used:
            raise GuardViolation("policy", "The query must read from at least one of the allowed tables.")
        return used

    def _check_pii_names(self, root: exp.Expression) -> None:
        pii = self.policy.all_pii_columns
        hits = sorted({c.name.lower() for c in root.find_all(exp.Column) if c.name.lower() in pii})
        if hits:
            raise GuardViolation(
                "policy",
                f"Personal data columns are not available: {', '.join(hits)}.",
                "Analyse customers in aggregate (counts, spend) by age band, gender, state, country "
                "or traffic source instead; individual customers are identified only by a pseudonymous id.",
            )

    def _qualify(self, root: exp.Expression) -> exp.Expression:
        try:
            return qualify(
                root,
                schema=self.policy.safe_schema(),
                dialect="bigquery",
                validate_qualify_columns=True,
                quote_identifiers=True,
            )
        except OptimizeError as e:
            raise GuardViolation(
                "syntax",
                f"Could not resolve the query against the schema: {e}",
                "Use only columns listed by describe_schema; qualify ambiguous columns with a table alias.",
            ) from e

    # ------------------------------------------------------------------ lineage & k-anonymity
    def _column_class(self, col: exp.Column, scope: Scope, infos: dict[int, _ScopeInfo]) -> tuple[bool, bool]:
        """Return (is_raw_quasi, is_uid) for a column reference in `scope`."""
        src = scope.sources.get(col.table)
        name = col.name.lower()
        if isinstance(src, exp.Table):
            base = src.name.lower()
            return name in self.policy.quasi.get(base, ()), name in self.policy.person_id.get(base, ())
        if isinstance(src, Scope):
            info = infos.get(id(src.expression))
            if info:
                return name in info.raw_quasi, name in info.uid
        return False, False

    def _uid_expr(self, scope: Scope, infos: dict[int, _ScopeInfo]) -> exp.Expression | None:
        for alias, src in scope.selected_sources.items():
            node, source = src
            if isinstance(source, exp.Table):
                base = source.name.lower()
                for c in sorted(self.policy.person_id.get(base, ())):
                    return exp.column(c, table=alias, quoted=True)
        for alias, (_node, source) in scope.selected_sources.items():
            if isinstance(source, Scope):
                info = infos.get(id(source.expression))
                if info and info.uid:
                    return exp.column(sorted(info.uid)[0], table=alias, quoted=True)
        return None

    @staticmethod
    def _inside(node: exp.Expression, kinds: tuple[type, ...], stop: exp.Expression) -> bool:
        p = node.parent
        while p is not None and p is not stop:
            if isinstance(p, kinds):
                return True
            p = p.parent
        return False

    @staticmethod
    def _compared_to_literal(col: exp.Column) -> bool:
        """True for `id = 123`, `id IN (1, 2)`, `id BETWEEN 1 AND 3`, `id > 10` (wrapping casts ignored)."""
        node: exp.Expression = col
        while isinstance(node.parent, (exp.Cast, exp.TryCast, exp.Paren)):
            node = node.parent
        cmp = node.parent
        if not isinstance(cmp, _COMPARISONS):
            return False
        others = [v for k, v in cmp.args.items() if v is not None and v is not node and k != "query"]
        flat: list[exp.Expression] = []
        for o in others:
            flat.extend(o if isinstance(o, list) else [o])
        return any(isinstance(o, exp.Literal) or (isinstance(o, exp.Expression) and o.find(exp.Literal) and
                                                  not o.find(exp.Column)) for o in flat)

    def _lineage_and_k(self, root: exp.Expression) -> tuple[int, set[str], list[str]]:
        infos: dict[int, _ScopeInfo] = {}
        k_scopes = 0
        notes: list[str] = []
        root_scope: Scope | None = None
        any_quasi = False
        singling_out: list[str] = []
        for scope in traverse_scope(root):
            info = _ScopeInfo()
            node = scope.expression
            if scope.is_root:
                root_scope = scope
            if isinstance(node, exp.SetOperation):
                left = infos.get(id(node.left)) or _ScopeInfo()
                right = infos.get(id(node.right)) or _ScopeInfo()
                info.raw_quasi = left.raw_quasi | right.raw_quasi
                info.uid = left.uid | right.uid
                info.tainted = left.tainted or right.tainted
                infos[id(node)] = info
                continue
            if not isinstance(node, exp.Select):
                infos[id(node)] = info
                continue

            # taint from children (derived tables, CTE refs, sub-queries in WHERE/SELECT)
            children = [s for s in scope.sources.values() if isinstance(s, Scope)] + list(scope.subquery_scopes)
            tainted = any((infos.get(id(c.expression)) or _ScopeInfo()).tainted for c in children)

            # BigQuery lets `SELECT u` / TO_JSON_STRING(u) turn a whole row into a STRUCT, which would
            # smuggle every column (incl. quasi-identifiers) past column-level lineage.
            # (sqlglot's qualifier turns such references into exp.TableColumn).
            for whole_row in node.find_all(exp.TableColumn):
                if whole_row:
                    raise GuardViolation(
                        "policy",
                        f"Whole-row reference '{whole_row.name}' is not allowed.",
                        "Select explicit columns instead of a table alias.",
                    )

            touches_quasi = False
            for col in scope.columns:
                q, u = self._column_class(col, scope, infos)
                touches_quasi |= q
                if (q or u) and self._inside(col, _LISTING_AGGS, node):
                    raise GuardViolation(
                        "policy",
                        f"Customer ids and demographic attributes cannot be listed inside ARRAY_AGG / STRING_AGG / "
                        f"APPROX_TOP_* / APPROX_QUANTILES (column '{col.name}').",
                        "Use counts or sums per group instead, e.g. COUNT(DISTINCT user_id), or GROUP BY the attribute.",
                    )
                if u and self._compared_to_literal(col):
                    singling_out.append(col.name)
            any_quasi |= touches_quasi

            is_agg = bool(node.args.get("group")) or any(
                isinstance(a, exp.AggFunc) and not self._inside(a, (exp.Window,), node)
                for sel in node.selects
                for a in sel.find_all(exp.AggFunc)
            )

            for sel in node.selects:
                out_name = sel.alias_or_name.lower()
                for col in sel.find_all(exp.Column):
                    q, u = self._column_class(col, scope, infos)
                    aggregated = self._inside(col, (exp.AggFunc,), sel)
                    if q and not aggregated:
                        info.raw_quasi.add(out_name)
                    if u and not aggregated:
                        info.uid.add(out_name)

            needs_k = is_agg and (touches_quasi or tainted)
            if needs_k:
                uid = self._uid_expr(scope, infos)
                if uid is None:
                    raise GuardViolation(
                        "policy",
                        "Demographic breakdowns must be computed from customer-level data so that groups "
                        f"smaller than {self.policy.k_min} customers can be suppressed.",
                        "Join users / orders / order_items in the same SELECT that does the GROUP BY.",
                    )
                cond = exp.GTE(
                    this=exp.Count(this=exp.Distinct(expressions=[uid])),
                    expression=exp.Literal.number(self.policy.k_min),
                )
                having = node.args.get("having")
                node.set("having", exp.Having(this=exp.and_(having.this, cond) if having else cond))
                k_scopes += 1
                # rows of an enforced aggregate are k-anonymous: consumers may use them freely
                info.raw_quasi.clear()
                info.tainted = False
            else:
                info.tainted = tainted or touches_quasi
                if is_agg:
                    info.raw_quasi.clear()
            infos[id(node)] = info

        if any_quasi and singling_out:
            # e.g. MAX(CASE WHEN u.id = 123 THEN u.age END) over a large group: the aggregate passes
            # k-anonymity but reveals one person's attribute.
            raise GuardViolation(
                "policy",
                "Queries that use demographic attributes cannot filter on specific customer ids "
                f"({', '.join(sorted(set(singling_out)))}).",
                "Analyse demographics for groups of customers, not for an individual customer.",
            )
        if root_scope is not None:
            final = infos.get(id(root_scope.expression)) or _ScopeInfo()
            if final.raw_quasi:
                raise GuardViolation(
                    "policy",
                    "Demographic attributes (" + ", ".join(sorted(final.raw_quasi)) + ") can only be shown in aggregates.",
                    "GROUP BY the attribute and aggregate (COUNT(DISTINCT user_id), SUM(sale_price), ...).",
                )
            if final.tainted:
                raise GuardViolation(
                    "policy",
                    "Row-level results filtered by demographic attributes are not allowed.",
                    "Aggregate the result (GROUP BY + COUNT/SUM/AVG) instead of listing individual rows.",
                )
            uid_cols = set(final.uid)
        else:  # pragma: no cover
            uid_cols = set()
        if k_scopes:
            notes.append(f"groups with fewer than {self.policy.k_min} customers are suppressed")
        return k_scopes, uid_cols, notes

    # ------------------------------------------------------------------ rewrite
    def _physical(self, table: str) -> exp.Table:
        parts = self.physical_table(table).split(".")
        ident = [exp.to_identifier(p, quoted=True) for p in parts]
        if len(ident) == 3:
            return exp.Table(this=ident[2], db=ident[1], catalog=ident[0])
        if len(ident) == 2:
            return exp.Table(this=ident[1], db=ident[0])
        return exp.Table(this=ident[0])

    def _entitled_product_ids(self, ent: Entitlement) -> exp.Select:
        cond = None
        for col, values in ent.filters.items():
            c = exp.In(this=exp.column(col, quoted=True), expressions=[exp.Literal.string(v) for v in values])
            cond = c if cond is None else exp.and_(cond, c)
        q = exp.select(exp.column("id", quoted=True)).from_(self._physical("products"))
        return q.where(cond) if cond is not None else q

    def _policy_view(self, table: str, ent: Entitlement) -> exp.Select:
        cols = [exp.column(c, quoted=True) for c in self.policy.safe_columns(table)]
        q = exp.select(*cols).from_(self._physical(table))
        if ent.unrestricted:
            return q
        prod_ids = self._entitled_product_ids(ent)

        def items_select(col: str) -> exp.Subquery:
            """SELECT <col> FROM order_items WHERE product_id IN (<entitled product ids>)"""
            return exp.Subquery(
                this=exp.select(exp.column(col, quoted=True))
                .from_(self._physical("order_items"))
                .where(exp.In(this=exp.column("product_id", quoted=True), query=exp.Subquery(this=prod_ids.copy())))
            )

        if table == "products":
            for col, values in ent.filters.items():
                q = q.where(exp.In(this=exp.column(col, quoted=True), expressions=[exp.Literal.string(v) for v in values]))
        elif table == "order_items":
            q = q.where(exp.In(this=exp.column("product_id", quoted=True), query=exp.Subquery(this=prod_ids)))
        elif table == "orders":
            q = q.where(exp.In(this=exp.column("order_id", quoted=True), query=items_select("order_id")))
        elif table == "users":
            q = q.where(exp.In(this=exp.column("id", quoted=True), query=items_select("user_id")))
        return q

    def _rewrite_tables(self, root: exp.Expression, cte_names: set[str], ent: Entitlement) -> None:
        allowed = set(self.policy.allowed_tables)
        for t in list(root.find_all(exp.Table)):
            name = t.name.lower()
            if t.db or name not in allowed or name in cte_names:
                continue
            alias = t.alias or name
            sub = exp.Subquery(this=self._policy_view(name, ent), alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
            t.replace(sub)

    def _enforce_limit(self, root: exp.Expression) -> int:
        cap = self.max_rows + 1  # +1 lets the caller detect truncation
        limit = root.args.get("limit")
        current = None
        if limit is not None:
            try:
                current = int(limit.expression.name if hasattr(limit, "expression") and limit.expression else limit.name)
            except (ValueError, AttributeError):
                current = None
        if current is None or current > cap:
            root.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
            return cap
        return current
