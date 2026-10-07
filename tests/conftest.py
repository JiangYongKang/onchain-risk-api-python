"""Shared fixtures: deterministic clock, service, HTTP client."""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.service import RiskService
from app.store import Store


@pytest.fixture
def fake_clock():
    """Deterministic monotonic epoch-us clock, 1000us per tick."""
    counter = itertools.count(1000, 100)
    return lambda: next(counter)


@pytest.fixture
def svc(fake_clock):
    return RiskService(Store(clock=fake_clock))


@pytest.fixture
def client(svc):
    app = create_app(service=svc)
    return TestClient(app)
