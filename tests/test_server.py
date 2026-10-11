import json

import pytest
from mcp import Client

from ec_orders_mcp.server import create_server

pytestmark = pytest.mark.anyio


async def test_tools_are_listed_as_read_only(db_path):
    async with Client(create_server(db_path)) as client:
        tools = (await client.list_tools()).tools
        names = {t.name for t in tools}
        assert names == {"describe_schema", "search_orders", "get_order", "sales_summary", "top_products", "run_sql"}
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)


async def test_call_tool_over_mcp(db_path):
    async with Client(create_server(db_path)) as client:
        result = await client.call_tool(
            "sales_summary", {"date_from": "2025-01-01", "date_to": "2025-01-31", "group_by": "channel"}
        )
        assert not result.is_error
        assert {"bucket", "orders", "revenue"} <= set(json.loads(result.content[0].text))


async def test_rejections_reach_the_model_as_tool_errors(db_path):
    async with Client(create_server(db_path)) as client:
        result = await client.call_tool("run_sql", {"query": "SELECT email FROM customers"})
        assert result.is_error and "Personal data" in result.content[0].text


async def test_resource_and_prompt(db_path):
    async with Client(create_server(db_path)) as client:
        resources = (await client.list_resources()).resources
        assert [str(r.uri) for r in resources] == ["schema://tables"]
        prompts = (await client.list_prompts()).prompts
        assert [p.name for p in prompts] == ["monthly_report"]


async def test_stdio_transport_end_to_end(db_path):
    """Launch the server the way Claude Desktop does: as a subprocess over stdio."""
    import sys

    from mcp import StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "ec_orders_mcp"], env={"EC_MCP_DB": str(db_path), "PATH": ""}
    )
    async with Client(params) as client:
        result = await client.call_tool("get_order", {"order_id": "ORD-10042"})
        order = json.loads(result.content[0].text)
        assert order["order_id"] == "ORD-10042" and "***" in order["customer"]["email"]
