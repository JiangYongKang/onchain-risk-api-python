import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.service import RiskService

T0 = 1_759_000_000_000


class StepClock:
    """Deterministic clock that only advances when asked."""

    def __init__(self, start=T0):
        self.t = start

    def now_ms(self):
        return self.t

    def advance(self, ms):
        self.t += ms
        return self.t


@pytest.fixture
def clock():
    return StepClock()


@pytest.fixture
def service(clock):
    return RiskService(clock=clock)


@pytest.fixture
def client(service):
    return TestClient(create_app(service), raise_server_exceptions=False)


def tx(h, frm="0xa1aaaa", to="0xb1bbbb", amount=10, ts=T0):
    return {"tx_hash": h, "from_addr": frm, "to_addr": to,
            "amount": amount, "ts_ms": ts}
