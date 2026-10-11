import pytest

from ec_orders_mcp.data import build
from ec_orders_mcp.store import OrderStore


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    return build(tmp_path_factory.mktemp("data") / "orders.db")


@pytest.fixture
def store(db_path, tmp_path):
    return OrderStore(db_path, audit_log=tmp_path / "audit.jsonl")


@pytest.fixture
def anyio_backend():
    return "asyncio"
