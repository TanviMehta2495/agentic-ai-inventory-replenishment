import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import AIReplenishmentAgent  # noqa: E402
from app.db import connect, init_schema  # noqa: E402
from app.seed import seed  # noqa: E402

MANAGER, WAREHOUSE = 1, 2


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    seed(c)
    yield c
    c.close()


@pytest.fixture
def agent(conn):
    return AIReplenishmentAgent(conn)


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def stock(conn, pid):
    return one(conn, "SELECT CurrentStock FROM Inventory WHERE ProductID=?", pid)[0]
