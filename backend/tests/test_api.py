"""API tests for the contact center backend.

Run against real PostgreSQL + Redis (service containers in CI, or local
containers for development). Asterisk is intentionally absent: the ARI
connection loop retries in the background and never blocks the API.
"""
import os

# Environment must be set before importing the app (module-level config).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://contactcenter:contactcenter@localhost:5432/contactcenter",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ARI_BASE", "http://ariuser:aripass@localhost:8088/ari")
os.environ.setdefault("ARI_APP", "ivr-test")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["redis"] is True


def test_seed_agents_present(client):
    agents = client.get("/agents").json()
    by_ext = {a["extension"]: a for a in agents}
    assert "6001" in by_ext and "6002" in by_ext


def test_queue_empty(client):
    q = client.get("/queue").json()
    assert q == {"sales": [], "support": []}


def test_create_agent_and_duplicate(client):
    r = client.post("/agents", json={"name": "Carol", "extension": "6003"})
    assert r.status_code == 201
    assert r.json()["state"] == "offline"

    r2 = client.post("/agents", json={"name": "Clone", "extension": "6003"})
    assert r2.status_code == 409


def test_agent_status_transitions(client):
    r = client.post("/agents/1/status", json={"state": "available"})
    assert r.status_code == 200
    assert r.json()["state"] == "available"

    r = client.post("/agents/1/status", json={"state": "offline"})
    assert r.json()["state"] == "offline"

    r = client.post("/agents/1/status", json={"state": "busy"})
    assert r.status_code == 400  # busy is system-managed

    r = client.post("/agents/999/status", json={"state": "available"})
    assert r.status_code == 404


def test_answer_unknown_call(client):
    r = client.post(
        "/queue/sales/nosuchcall/answer", json={"agent_id": 1}
    )
    assert r.status_code == 404


def test_answer_unknown_queue(client):
    r = client.post(
        "/queue/bogus/abc/answer", json={"agent_id": 1}
    )
    assert r.status_code == 404


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "cc_calls_total" in r.text
    assert "cc_wait_seconds" in r.text
