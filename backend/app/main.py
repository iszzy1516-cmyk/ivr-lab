"""Contact center MVP — FastAPI app.

Routes:
  GET  /                     agent dashboard (static)
  GET  /healthz              liveness
  GET  /queue                calls currently waiting per queue
  GET  /agents               registered agents + state
  POST /agents               register an agent {name, extension}
  POST /agents/{id}/status   set state: available | offline  (busy is system-managed)
  GET  /calls                recent call history (Postgres)
  POST /queue/{q}/{key}/answer   pick up a queued call as an agent {agent_id}
  WS   /events               live call/agent events
  GET  /metrics              Prometheus scrape endpoint
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import ari, db
from .core import K_AGENTS, broker, k_call, k_queue, list_agents, redis, set_agent_state, QUEUE_NAMES

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    await seed_agents()
    ari_task = asyncio.create_task(ari.connect_forever())
    log.info("backend started")
    yield
    ari_task.cancel()


app = FastAPI(title="ivr-lab contact center", lifespan=lifespan)


async def seed_agents():
    """Two demo agents matching the provisioned softphones 6001/6002."""
    for name, ext in (("Alice", "6001"), ("Bob", "6002")):
        row = await db.run(
            "INSERT INTO agents (name, extension, state) VALUES (%s,%s,'offline')"
            " ON CONFLICT (extension) DO NOTHING RETURNING id",
            (name, ext), fetch="one",
        )
        if row:
            from .core import save_agent
            await save_agent({"id": row[0], "name": name,
                              "extension": ext, "state": "offline"})
    # make sure redis mirrors the DB (covers container restarts)
    rows = await db.run("SELECT id, name, extension, state FROM agents ORDER BY id",
                        fetch="all")
    for aid, name, ext, state in rows:
        cur = await redis.hget(K_AGENTS, str(aid))
        if not cur:
            from .core import save_agent
            await save_agent({"id": aid, "name": name,
                              "extension": ext, "state": state})


@app.get("/healthz")
async def healthz():
    try:
        await redis.ping()
        pong = True
    except Exception:
        pong = False
    return {"ok": True, "redis": pong}


@app.get("/queue")
async def get_queue():
    out = {}
    for q in QUEUE_NAMES:
        keys = await redis.lrange(k_queue(q), 0, -1)
        items = []
        for k in keys:
            h = await redis.hgetall(k_call(k))
            if h:
                items.append({
                    "key": k,
                    "caller": h.get("caller"),
                    "status": h.get("status"),
                    "waited": round(time.time() - float(h.get("enqueue_ts", time.time())), 1),
                })
        out[q] = items
    return out


@app.get("/agents")
async def get_agents():
    return await list_agents()


class AgentIn(BaseModel):
    name: str
    extension: str


@app.post("/agents", status_code=201)
async def create_agent(body: AgentIn):
    row = await db.run(
        "INSERT INTO agents (name, extension) VALUES (%s,%s)"
        " ON CONFLICT (extension) DO NOTHING RETURNING id",
        (body.name, body.extension), fetch="one",
    )
    if not row:
        raise HTTPException(409, "extension already registered")
    agent = {"id": row[0], "name": body.name,
             "extension": body.extension, "state": "offline"}
    from .core import save_agent
    await save_agent(agent)
    await broker.publish({"type": "agent_update", "agent": agent})
    return agent


class StatusIn(BaseModel):
    state: str  # available | offline


@app.post("/agents/{agent_id}/status")
async def set_status(agent_id: int, body: StatusIn):
    if body.state not in ("available", "offline"):
        raise HTTPException(400, "state must be 'available' or 'offline'")
    agent = await set_agent_state(agent_id, body.state)
    if not agent:
        raise HTTPException(404, "agent not found")
    return agent


@app.get("/calls")
async def get_calls(limit: int = 50):
    rows = await db.run(
        "SELECT call_key, caller_id, queue, status, outcome, enqueued_at,"
        " answered_at, ended_at, wait_seconds, handle_seconds"
        " FROM calls ORDER BY id DESC LIMIT %s",
        (min(limit, 500),), fetch="all",
    )
    cols = ("call_key", "caller_id", "queue", "status", "outcome",
            "enqueued_at", "answered_at", "ended_at",
            "wait_seconds", "handle_seconds")
    return [dict(zip(cols, [str(v) if v is not None else None for v in row]))
            for row in rows]


class AnswerIn(BaseModel):
    agent_id: int


@app.post("/queue/{queue}/{key}/answer")
async def pick_up(queue: str, key: str, body: AnswerIn):
    if queue not in QUEUE_NAMES:
        raise HTTPException(404, "unknown queue")
    try:
        await ari.answer_call(queue, key, body.agent_id)
    except KeyError:
        raise HTTPException(404, "call not in queue anymore")
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"agent originate failed: {e}")
    return {"ok": True, "key": key, "agent_id": body.agent_id}


@app.websocket("/events")
async def events(ws: WebSocket):
    await ws.accept()
    q = broker.subscribe()
    try:
        while True:
            msg = await q.get()
            await ws.send_text(json.dumps(msg))
    except WebSocketDisconnect:
        pass
    finally:
        broker.unsubscribe(q)


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__),
                                                  "static"), html=True),
          name="static")
