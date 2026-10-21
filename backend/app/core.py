"""Shared runtime pieces: config, Redis handle, metrics, WebSocket broker."""
import asyncio
import json
import os

import redis.asyncio as aioredis
from prometheus_client import Counter, Histogram

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
QUEUE_NAMES = ("sales", "support")

redis = aioredis.from_url(REDIS_URL, decode_responses=True)

K_AGENTS = "cc:agents"  # hash: agent_id -> json {id, name, extension, state}


def k_queue(queue: str) -> str:
    return f"cc:queue:{queue}"


def k_call(key: str) -> str:
    return f"cc:call:{key}"


# --- metrics -----------------------------------------------------------------

CALLS_TOTAL = Counter(
    "cc_calls_total", "Finalized calls", ["queue", "outcome"]
)
WAIT_SECONDS = Histogram(
    "cc_wait_seconds",
    "Seconds in queue before answered or abandoned",
    ["queue"],
    buckets=[5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300],
)
HANDLE_SECONDS = Histogram(
    "cc_handle_seconds",
    "Seconds of talk time on answered calls",
    ["queue"],
    buckets=[10, 30, 60, 120, 180, 300, 600, 1200],
)

# --- WebSocket event broker ---------------------------------------------------


class Broker:
    """Fan-out of call/agent events to every connected dashboard."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def publish(self, msg: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


broker = Broker()


# --- agent state helpers -------------------------------------------------------


async def get_agent(agent_id) -> dict | None:
    raw = await redis.hget(K_AGENTS, str(agent_id))
    return json.loads(raw) if raw else None


async def list_agents() -> list[dict]:
    raw = await redis.hgetall(K_AGENTS)
    agents = [json.loads(v) for v in raw.values()]
    agents.sort(key=lambda a: a["id"])
    return agents


async def save_agent(agent: dict) -> None:
    await redis.hset(K_AGENTS, str(agent["id"]), json.dumps(agent))


async def set_agent_state(agent_id, state: str) -> dict | None:
    agent = await get_agent(agent_id)
    if not agent:
        return None
    agent["state"] = state
    await save_agent(agent)
    from . import db  # local import to avoid a cycle

    await db.run("UPDATE agents SET state=%s, updated_at=now() WHERE id=%s",
                 (state, agent_id))
    await broker.publish({"type": "agent_update", "agent": agent})
    return agent
