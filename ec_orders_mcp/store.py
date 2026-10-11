"""Read-only access to the orders database.

Safety is layered so that one mistake does not expose data or change it:

1. The SQLite file is opened with ``mode=ro`` (the engine itself refuses writes).
2. An authorizer allows only SELECT and reads of known tables, and blocks the
   personal-data columns (``customers.email``, ``customers.phone``,
   ``customers.name``) for free-form SQL.
3. Free-form SQL is wrapped in an outer ``LIMIT`` and stopped after a time budget.
4. Fixed tools return customer data only in masked form.
5. Every call is written to an audit log (arguments and row counts, never results).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ALLOWED_TABLES = {"customers", "products", "orders", "order_items"}
PII_COLUMNS = {("customers", "email"), ("customers", "phone"), ("customers", "name")}
MAX_ROWS = 200
TIME_BUDGET_SECONDS = 2.0

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit"}


class QueryRejected(ValueError):
    """Raised when a query is not allowed. The message is safe to show to the model."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool

    def as_dict(self) -> dict:
        return {"columns": self.columns, "rows": self.rows, "row_count": len(self.rows), "truncated": self.truncated}


def mask_email(email: str) -> str:
    """customer00012@example.com -> c***2@example.com"""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[0]}***{local[-1]}@{domain}" if len(local) > 1 else f"*@{domain}"


def mask_phone(phone: str) -> str:
    """090-1234-5678 -> ***-****-5678"""
    digits = re.sub(r"\D", "", phone)
    return f"***-****-{digits[-4:]}" if len(digits) >= 4 else "***"


def _authorizer(action, arg1, arg2, db_name, trigger):
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        table, column = (arg1 or "").lower(), (arg2 or "").lower()
        # Internal catalog tables and personal-data columns are off limits. Other
        # names are either the allowed tables or CTEs defined inside the query
        # (the database file is read-only and contains nothing else).
        if table.startswith("sqlite_") or (table, column) in PII_COLUMNS:
            return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class OrderStore:
    def __init__(self, db_path: str | Path, audit_log: str | Path | None = None):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"database not found: {self.db_path}")
        self.audit_log = Path(audit_log) if audit_log else None

    # -- connection helpers -------------------------------------------------

    @contextmanager
    def _connect(self, guarded: bool = False):
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        if guarded:
            deadline = time.monotonic() + TIME_BUDGET_SECONDS
            con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            con.set_authorizer(_authorizer)
        try:
            yield con
        finally:
            con.close()

    def _audit(self, tool: str, args: dict, rows: int, started: float, error: str | None = None) -> None:
        if not self.audit_log:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "tool": tool,
            "args": args,
            "rows": rows,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }
        if error:
            entry["error"] = error
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _select(self, tool: str, args: dict, sql: str, params: tuple = ()) -> list[dict]:
        started = time.perf_counter()
        with self._connect() as con:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        self._audit(tool, args, len(rows), started)
        return rows

    # -- fixed, parameterised tools ----------------------------------------

    def describe_schema(self) -> dict:
        with self._connect() as con:
            tables = {}
            for table in sorted(ALLOWED_TABLES):
                cols = con.execute(f"PRAGMA table_info({table})").fetchall()
                tables[table] = [
                    {"name": c["name"], "type": c["type"], "restricted": (table, c["name"]) in PII_COLUMNS}
                    for c in cols
                ]
        return {
            "tables": tables,
            "notes": [
                "Dates are stored as 'YYYY-MM-DD HH:MM' text in orders.ordered_at.",
                "Revenue = SUM(order_items.quantity * order_items.unit_price) for orders with status 'completed'.",
                "Columns marked restricted cannot be selected with run_sql.",
            ],
        }

    def search_orders(
        self,
        status: str | None = None,
        channel: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        customer_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        args = {k: v for k, v in locals().items() if k != "self" and v is not None}
        limit = max(1, min(int(limit), 100))
        where, params = [], []
        for column, value in (("o.status", status), ("o.channel", channel), ("o.customer_id", customer_id)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        if date_from:
            where.append("o.ordered_at >= ?")
            params.append(date_from)
        if date_to:
            where.append("o.ordered_at < date(?, '+1 day')")
            params.append(date_to)
        sql = f"""
            SELECT o.order_id, o.ordered_at, o.channel, o.status, o.customer_id,
                   SUM(i.quantity * i.unit_price) AS merchandise_amount, o.shipping_fee
            FROM orders o JOIN order_items i ON i.order_id = o.order_id
            {"WHERE " + " AND ".join(where) if where else ""}
            GROUP BY o.order_id ORDER BY o.ordered_at DESC LIMIT ?
        """
        return self._select("search_orders", args, sql, (*params, limit))

    def get_order(self, order_id: str) -> dict | None:
        started = time.perf_counter()
        with self._connect() as con:
            head = con.execute(
                """SELECT o.*, c.name, c.email, c.phone, c.prefecture
                   FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id = ?""",
                (order_id.strip().upper(),),
            ).fetchone()
            if head is None:
                self._audit("get_order", {"order_id": order_id}, 0, started)
                return None
            lines = con.execute(
                """SELECT i.product_id, p.product_name, p.category, i.quantity, i.unit_price,
                          i.quantity * i.unit_price AS line_amount
                   FROM order_items i JOIN products p ON p.product_id = i.product_id
                   WHERE i.order_id = ? ORDER BY i.order_item_id""",
                (head["order_id"],),
            ).fetchall()
        self._audit("get_order", {"order_id": order_id}, 1, started)
        merchandise = sum(r["line_amount"] for r in lines)
        return {
            "order_id": head["order_id"],
            "ordered_at": head["ordered_at"],
            "channel": head["channel"],
            "status": head["status"],
            "customer": {
                "customer_id": head["customer_id"],
                "name": head["name"].split()[0][0] + ". " + head["name"].split()[-1][0] + ".",
                "email": mask_email(head["email"]),
                "phone": mask_phone(head["phone"]),
                "prefecture": head["prefecture"],
            },
            "lines": [dict(r) for r in lines],
            "merchandise_amount": merchandise,
            "shipping_fee": head["shipping_fee"],
            "total": merchandise + head["shipping_fee"],
        }

    def sales_summary(self, date_from: str, date_to: str, group_by: str = "month") -> list[dict]:
        groups = {
            "day": "substr(o.ordered_at, 1, 10)",
            "week": "strftime('%Y-W%W', o.ordered_at)",
            "month": "substr(o.ordered_at, 1, 7)",
            "channel": "o.channel",
            "category": "p.category",
        }
        if group_by not in groups:
            raise QueryRejected(f"group_by must be one of {sorted(groups)}")
        key = groups[group_by]
        sql = f"""
            SELECT {key} AS bucket,
                   COUNT(DISTINCT o.order_id) AS orders,
                   SUM(i.quantity * i.unit_price) AS revenue,
                   ROUND(1.0 * SUM(i.quantity * i.unit_price) / COUNT(DISTINCT o.order_id)) AS average_order_value
            FROM orders o
            JOIN order_items i ON i.order_id = o.order_id
            JOIN products p ON p.product_id = i.product_id
            WHERE o.status = 'completed' AND o.ordered_at >= ? AND o.ordered_at < date(?, '+1 day')
            GROUP BY bucket ORDER BY {"revenue DESC" if group_by in ("channel", "category") else "bucket"}
        """
        return self._select(
            "sales_summary",
            {"date_from": date_from, "date_to": date_to, "group_by": group_by},
            sql,
            (date_from, date_to),
        )

    def top_products(
        self, date_from: str | None = None, date_to: str | None = None, metric: str = "revenue", limit: int = 10
    ) -> list[dict]:
        if metric not in ("revenue", "quantity"):
            raise QueryRejected("metric must be 'revenue' or 'quantity'")
        limit = max(1, min(int(limit), 50))
        where, params = ["o.status = 'completed'"], []
        if date_from:
            where.append("o.ordered_at >= ?")
            params.append(date_from)
        if date_to:
            where.append("o.ordered_at < date(?, '+1 day')")
            params.append(date_to)
        sql = f"""
            SELECT p.product_id, p.product_name, p.category,
                   SUM(i.quantity) AS quantity, SUM(i.quantity * i.unit_price) AS revenue
            FROM order_items i
            JOIN orders o ON o.order_id = i.order_id
            JOIN products p ON p.product_id = i.product_id
            WHERE {" AND ".join(where)}
            GROUP BY p.product_id ORDER BY {metric} DESC LIMIT ?
        """
        return self._select(
            "top_products",
            {"date_from": date_from, "date_to": date_to, "metric": metric, "limit": limit},
            sql,
            (*params, limit),
        )

    # -- free-form SQL, guarded ---------------------------------------------

    def run_sql(self, query: str, max_rows: int = MAX_ROWS) -> QueryResult:
        started = time.perf_counter()
        cleaned = query.strip().rstrip(";").strip()
        try:
            if not re.match(r"(?is)^(select|with)\b", cleaned):
                raise QueryRejected("Only a single SELECT (or WITH ... SELECT) statement is allowed.")
            if ";" in cleaned:
                raise QueryRejected("Only one statement per call is allowed.")
            max_rows = max(1, min(int(max_rows), MAX_ROWS))
            wrapped = f"SELECT * FROM ({cleaned}) LIMIT {max_rows + 1}"
            with self._connect(guarded=True) as con:
                try:
                    cur = con.execute(wrapped)
                    rows = cur.fetchall()
                except sqlite3.DatabaseError as exc:
                    msg = str(exc)
                    if "not authorized" in msg or "prohibited" in msg:
                        raise QueryRejected(
                            "Query touches a restricted table, column or function. "
                            "Personal data (customers.name, email, phone) is not available through SQL."
                        ) from None
                    if "interrupted" in msg:
                        raise QueryRejected(f"Query exceeded the {TIME_BUDGET_SECONDS:.0f}s time budget.") from None
                    raise QueryRejected(f"SQL error: {msg}") from None
                columns = [d[0] for d in cur.description]
        except QueryRejected as exc:
            self._audit("run_sql", {"query": cleaned}, 0, started, error=str(exc))
            raise
        truncated = len(rows) > max_rows
        result = QueryResult(columns, [list(r) for r in rows[:max_rows]], truncated)
        self._audit("run_sql", {"query": cleaned}, len(result.rows), started)
        return result
