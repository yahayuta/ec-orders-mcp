import json

import pytest

from ec_orders_mcp.store import QueryRejected, mask_email, mask_phone


def test_masking():
    assert mask_email("customer00012@example.com") == "c***2@example.com"
    assert mask_phone("090-1234-5678") == "***-****-5678"


def test_get_order_masks_personal_data(store):
    order = store.get_order("ord-10042")  # case-insensitive lookup
    customer = order["customer"]
    assert "@example.com" in customer["email"] and "***" in customer["email"]
    assert customer["phone"].startswith("***-****-")
    assert order["total"] == order["merchandise_amount"] + order["shipping_fee"]
    assert store.get_order("ORD-1") is None


def test_search_orders_filters(store):
    rows = store.search_orders(status="cancelled", channel="own_store", date_from="2025-03-01", date_to="2025-03-31")
    assert rows and all(r["status"] == "cancelled" and r["channel"] == "own_store" for r in rows)
    assert all("2025-03-01" <= r["ordered_at"] < "2025-04-01" for r in rows)
    assert len(store.search_orders(limit=1000)) == 100  # capped


def test_summary_matches_sql(store):
    by_channel = store.sales_summary("2025-01-01", "2025-12-31", "channel")
    by_month = store.sales_summary("2025-01-01", "2025-12-31", "month")
    assert sum(r["revenue"] for r in by_channel) == sum(r["revenue"] for r in by_month)
    total = store.run_sql(
        "SELECT SUM(i.quantity * i.unit_price) FROM order_items i JOIN orders o USING (order_id) "
        "WHERE o.status = 'completed'"
    ).rows[0][0]
    assert total == sum(r["revenue"] for r in by_month)
    with pytest.raises(QueryRejected):
        store.sales_summary("2025-01-01", "2025-12-31", "year")


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM orders",
        "UPDATE orders SET status = 'x'",
        "SELECT 1; DROP TABLE orders",
        "PRAGMA table_info(orders)",
        "ATTACH DATABASE 'x.db' AS x",
        "SELECT email FROM customers",
        "SELECT * FROM customers",
        "SELECT name FROM customers WHERE customer_id = 'C00001'",
        "SELECT sql FROM sqlite_master",
        "SELECT load_extension('evil')",
    ],
)
def test_run_sql_rejects_unsafe_queries(store, query):
    with pytest.raises(QueryRejected):
        store.run_sql(query)


def test_run_sql_allows_analysis_and_ctes(store):
    result = store.run_sql(
        "WITH c AS (SELECT customer_id, prefecture FROM customers) "
        "SELECT prefecture, COUNT(*) AS n FROM c GROUP BY prefecture ORDER BY n DESC"
    )
    assert result.columns == ["prefecture", "n"] and result.rows


def test_run_sql_row_cap_and_time_budget(store):
    result = store.run_sql("SELECT order_id FROM orders")
    assert len(result.rows) == 200 and result.truncated
    with pytest.raises(QueryRejected, match="time budget"):
        store.run_sql("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r")


def test_database_is_opened_read_only(store):
    with store._connect() as con, pytest.raises(Exception, match="readonly"):
        con.execute("DELETE FROM orders")


def test_audit_log_records_calls_not_results(store):
    store.search_orders(status="completed", limit=3)
    with pytest.raises(QueryRejected):
        store.run_sql("SELECT email FROM customers")
    entries = [json.loads(line) for line in store.audit_log.read_text().splitlines()]
    assert [e["tool"] for e in entries] == ["search_orders", "run_sql"]
    assert entries[0]["rows"] == 3 and "error" in entries[1]
    assert "@example.com" not in store.audit_log.read_text()
