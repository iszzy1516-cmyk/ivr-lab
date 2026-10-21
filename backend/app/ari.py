"""ARI (Asterisk REST Interface) client + call-control logic.

Inbound flow:  softphone -> 9000 -> Stasis(ivr) -> handle_inbound()
  1. answer, play IVR prompt, collect DTMF (1=sales, 2=support)
  2. create a mixing bridge, park the caller there with music on hold
  3. push call into a Redis queue, broadcast to dashboards
  4. an agent clicks "Answer" -> we originate to their softphone ->
     agent answers -> both channels share the bridge -> talk
  5. either leg hangs up -> finalize: DB row + metrics + broadcast
"""
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from urllib.parse import urlparse

import httpx
import websockets

from . import db
from .core import (
    CALLS_TOTAL,
    HANDLE_SECONDS,
    WAIT_SECONDS,
    broker,
    k_call,
    k_queue,
    redis,
    set_agent_state,
    QUEUE_NAMES,
)

log = logging.getLogger("ari")

ARI_BASE = os.getenv("ARI_BASE", "http://ariuser:aripass@asterisk:8088/ari")
ARI_APP = os.getenv("ARI_APP", "ivr")

# Sound prompts (bundled core sounds, installed in the Asterisk image).
IVR_PROMPT = [m.strip() for m in os.getenv(
    "IVR_PROMPT",
    "sound:basic-pbx-ivr-main, sound:silence/1",
).split(",") if m.strip()]
IVR_INVALID = ["sound:pbx-invalid"]
IVR_GOODBYE = ["sound:vm-goodbye"]

_u = urlparse(ARI_BASE)
HTTP_BASE = f"{_u.scheme}://{_u.hostname}:{_u.port}/ari/"
WS_URL = f"ws://{_u.hostname}:{_u.port}/ari/events?app={ARI_APP}"
AUTH = (_u.username, _u.password)
_BASIC_AUTH = "Basic " + base64.b64encode(
    f"{_u.username}:{_u.password}".encode()).decode()


class Ari:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(base_url=HTTP_BASE, auth=AUTH, timeout=15)
        # coordination primitives, keyed by Asterisk channel id / call key
        self.dtmf: dict[str, asyncio.Queue] = {}
        self.hangups: dict[str, asyncio.Event] = {}
        self.answered: dict[str, asyncio.Event] = {}
        self.agent_legs: dict[str, tuple[str, int]] = {}  # ch -> (call_key, agent_id)
        self.finalized: set[str] = set()

    # -- REST helpers ----------------------------------------------------------

    async def _req(self, method: str, path: str, **kw):
        # Asterisk's built-in HTTP server occasionally drops keep-alive
        # connections under burst; retry a dropped request once. Worst case a
        # prompt plays twice on ~0.1% of calls — acceptable for a lab.
        last = None
        for attempt in (1, 2):
            try:
                r = await self.http.request(method, path, **kw)
            except httpx.TransportError as e:
                last = e
                log.warning("ARI %s %s transport error (attempt %d): %s",
                            method, path, attempt, e)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"ARI {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"ARI {method} {path} unreachable: {last}")

    async def answer(self, cid): return await self._req("POST", f"channels/{cid}/answer")

    async def hangup(self, cid):
        try:
            await self._req("DELETE", f"channels/{cid}")
        except Exception as e:
            log.debug("hangup %s failed: %s", cid, e)

    async def play(self, cid, media):
        return await self._req("POST", f"channels/{cid}/play",
                               params={"playId": f"p-{cid}"}, json={"media": media})

    async def bridge_create(self, name):
        b = await self._req("POST", "bridges",
                            json={"type": "mixing", "name": name})
        return b["id"]

    async def bridge_add(self, bid, cid):
        return await self._req("POST", f"bridges/{bid}/addChannel",
                               params={"channel": cid})

    async def bridge_del(self, bid):
        try:
            await self._req("DELETE", f"bridges/{bid}")
        except Exception as e:
            log.debug("bridge_del %s failed: %s", bid, e)

    async def moh_start(self, bid):
        try:
            await self._req("POST", f"bridges/{bid}/moh")
        except Exception as e:
            log.warning("MOH failed on bridge %s: %s", bid, e)

    async def moh_stop(self, bid):
        try:
            await self._req("DELETE", f"bridges/{bid}/moh")
        except Exception as e:
            log.debug("moh_stop %s failed: %s", bid, e)

    async def originate(self, endpoint, app_args, caller_id, timeout=30):
        return await self._req("POST", "channels", json={
            "endpoint": endpoint,
            "app": ARI_APP,
            "appArgs": app_args,
            "callerId": caller_id,
            "timeout": timeout,
        })


ari = Ari()


# --- event pump ----------------------------------------------------------------


async def connect_forever():
    """Maintain the ARI WebSocket, reconnecting with backoff forever."""
    while True:
        try:
            async with websockets.connect(WS_URL, max_queue=2048, additional_headers={
                "Authorization": _BASIC_AUTH,
            }) as ws:
                log.info("ARI WebSocket connected: %s", WS_URL)
                await broker.publish({"type": "ari_connected"})
                async for raw in ws:
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    asyncio.ensure_future(dispatch(ev))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("ARI WebSocket dropped (%s); reconnecting in 3s", e)
            await broker.publish({"type": "ari_disconnected", "error": str(e)})
            await asyncio.sleep(3)


async def dispatch(ev: dict):
    t = ev.get("type")
    if t == "StasisStart":
        ch = ev["channel"]
        args = ev.get("args") or []
        if args and args[0] == "agent":
            key = args[1] if len(args) > 1 else ""
            asyncio.ensure_future(handle_agent_leg(ch, key))
        else:
            direct_queue = args[1] if len(args) > 1 and args[1] in QUEUE_NAMES else None
            asyncio.ensure_future(handle_inbound(ch, direct_queue))
    elif t == "ChannelDtmfReceived":
        q = ari.dtmf.get(ev["channel"]["id"])
        if q is not None:
            q.put_nowait(ev.get("digit", ""))
    elif t == "ChannelStateChange":
        ch = ev["channel"]
        # an ARI-originated agent leg enters Stasis while still Ringing;
        # only bridge it once the softphone actually answers.
        if ch["id"] in ari.agent_legs and ch.get("state") == "Up":
            key = ari.agent_legs[ch["id"]][0]
            asyncio.ensure_future(complete_bridge(ch["id"], key))
    elif t == "StasisEnd":
        await handle_stasis_end(ev["channel"])


# --- inbound / IVR ---------------------------------------------------------------


async def wait_dtmf(cid: str, timeout: float):
    q = ari.dtmf.setdefault(cid, asyncio.Queue())
    try:
        return await asyncio.wait_for(q.get(), timeout)
    except asyncio.TimeoutError:
        return None


async def handle_inbound(ch: dict, direct_queue: str | None = None):
    cid = ch["id"]
    caller = (ch.get("caller") or {}).get("number") or "unknown"
    log.info("inbound call channel=%s caller=%s direct=%s", cid, caller, direct_queue)
    try:
        await ari.answer(cid)
        if direct_queue:
            await enqueue(cid, caller, direct_queue)
            return
        choice = None
        for attempt in range(2):
            await ari.play(cid, IVR_PROMPT)
            digit = await wait_dtmf(cid, 10)
            log.info("channel=%s dtmf=%r (attempt %d)", cid, digit, attempt)
            if digit in ("1", "2"):
                choice = digit
                break
            if digit is not None:
                await ari.play(cid, IVR_INVALID)
        if not choice:
            await ari.play(cid, IVR_GOODBYE)
            await asyncio.sleep(2)
            await ari.hangup(cid)
            await log_direct_call(cid, caller, "no_choice")
            return
        await enqueue(cid, caller, "sales" if choice == "1" else "support")
    except Exception as e:
        log.exception("handle_inbound failed for %s: %s", cid, e)
        await ari.hangup(cid)


async def log_direct_call(cid, caller, outcome):
    """Log calls that never reached a queue (IVR timeout / invalid)."""
    key = uuid.uuid4().hex[:12]
    await db.run(
        "INSERT INTO calls (call_key, channel_id, caller_id, status, outcome, ended_at)"
        " VALUES (%s,%s,%s,'ended',%s,now()) ON CONFLICT (call_key) DO NOTHING",
        (key, cid, caller, outcome),
    )
    CALLS_TOTAL.labels(queue="none", outcome=outcome).inc()
    await broker.publish({"type": "call_ended", "key": key, "queue": None,
                          "outcome": outcome, "wait": 0, "handle": 0})


async def enqueue(cid: str, caller: str, queue: str):
    key = uuid.uuid4().hex[:12]
    bid = await ari.bridge_create(key)
    await ari.bridge_add(bid, cid)
    await ari.moh_start(bid)
    now = time.time()

    await db.run(
        "INSERT INTO calls (call_key, channel_id, caller_id, queue, status, enqueued_at)"
        " VALUES (%s,%s,%s,%s,'queued',now()) ON CONFLICT (call_key) DO NOTHING",
        (key, cid, caller, queue),
    )
    await redis.hset(k_call(key), mapping={
        "channel": cid, "caller": caller, "queue": queue,
        "bridge": bid, "status": "queued", "enqueue_ts": str(now),
    })
    await redis.rpush(k_queue(queue), key)
    await broker.publish({"type": "call_queued", "key": key, "queue": queue,
                          "caller": caller, "waited": 0})
    log.info("call %s queued in %s (channel=%s)", key, queue, cid)

    ans = ari.answered.setdefault(key, asyncio.Event())
    hung = ari.hangups.setdefault(cid, asyncio.Event())
    await asyncio.wait(
        [asyncio.create_task(ans.wait()), asyncio.create_task(hung.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    if ans.is_set():
        # bridged: stick around until the caller hangs up so we can
        # record handle time and release the agent.
        await hung.wait()
    await finalize(key)


async def finalize(key: str):
    if key in ari.finalized:
        return
    ari.finalized.add(key)
    h = await redis.hgetall(k_call(key))
    if not h:
        return
    queue = h.get("queue", "unknown")
    status = h.get("status", "queued")
    outcome = "answered" if status == "answered" else "abandoned"

    ended = time.time()
    answered_ts = float(h.get("answered_ts") or 0)
    enqueue_ts = float(h.get("enqueue_ts") or ended)
    wait = max(0.0, (answered_ts or ended) - enqueue_ts)
    handle = max(0.0, ended - answered_ts) if status == "answered" else 0.0

    await db.run(
        "UPDATE calls SET status='ended', outcome=%s, ended_at=now(),"
        " wait_seconds=%s, handle_seconds=%s WHERE call_key=%s",
        (outcome, wait, handle, key),
    )
    CALLS_TOTAL.labels(queue=queue, outcome=outcome).inc()
    WAIT_SECONDS.labels(queue=queue).observe(wait)
    if status == "answered":
        HANDLE_SECONDS.labels(queue=queue).observe(handle)

    # tear the call down
    if status == "answered" and h.get("agent_channel"):
        await ari.hangup(h["agent_channel"])  # in case the agent is still around
    if h.get("bridge"):
        await ari.bridge_del(h["bridge"])
    if h.get("agent_id") and status == "answered":
        await set_agent_state(h["agent_id"], "available")
    await redis.delete(k_call(key))
    for qn in QUEUE_NAMES:
        await redis.lrem(k_queue(qn), 0, key)

    await broker.publish({
        "type": "call_ended", "key": key, "queue": queue,
        "outcome": outcome, "wait": round(wait, 1), "handle": round(handle, 1),
    })
    log.info("call %s finalized outcome=%s wait=%.1fs handle=%.1fs",
             key, outcome, wait, handle)


# --- agent answer flow ------------------------------------------------------------


async def answer_call(queue: str, key: str, agent_id: int):
    """Called from the dashboard: ring an agent's softphone for a queued call."""
    h = await redis.hgetall(k_call(key))
    if not h:
        raise KeyError(key)
    if h.get("status") != "queued":
        raise ValueError(f"call {key} is {h.get('status')}, not queued")
    from .core import get_agent  # late import: core imports db lazily already

    agent = await get_agent(agent_id)
    if not agent:
        raise ValueError(f"agent {agent_id} not found")
    if agent["state"] != "available":
        raise ValueError(f"agent {agent_id} is {agent['state']}")

    await redis.hset(k_call(key), mapping={"status": "ringing", "agent_id": str(agent_id)})
    await set_agent_state(agent_id, "busy")
    await broker.publish({"type": "call_ringing", "key": key, "queue": queue,
                          "agent_id": agent_id, "caller": h.get("caller")})
    try:
        leg = await ari.originate(
            f"PJSIP/{agent['extension']}",
            app_args=f"agent,{key}",
            caller_id=f"CC-{key}",
        )
        await redis.hset(k_call(key), "agent_channel", leg["id"])
        ari.agent_legs[leg["id"]] = (key, agent_id)
        log.info("ringing agent %s (%s) for call %s", agent_id, agent["extension"], key)
    except Exception:
        # roll back so another agent can take it
        await redis.hset(k_call(key), mapping={"status": "queued"})
        await redis.hdel(k_call(key), "agent_id")
        await set_agent_state(agent_id, "available")
        raise


async def handle_agent_leg(ch: dict, key: str):
    cid = ch["id"]
    h = await redis.hgetall(k_call(key))
    if not h or h.get("status") == "ended":
        log.warning("call %s gone; hanging up agent leg %s", key, cid)
        await ari.hangup(cid)
        return
    if cid not in ari.agent_legs:
        try:
            agent_id = int(h.get("agent_id") or 0)
        except ValueError:
            agent_id = 0
        ari.agent_legs[cid] = (key, agent_id)
    log.info("agent leg in Stasis channel=%s call=%s (state=%s)",
             cid, key, ch.get("state"))
    if ch.get("state") == "Up":
        await complete_bridge(cid, key)


async def complete_bridge(agent_cid: str, key: str):
    """Join the answered agent leg to the caller's bridge. Idempotent."""
    h = await redis.hgetall(k_call(key))
    if not h or h.get("status") not in ("queued", "ringing"):
        return
    if h.get("status") == "ringing" and h.get("agent_channel") != agent_cid:
        return  # a different leg is already handling this call
    await redis.hset(k_call(key), mapping={
        "status": "answered", "answered_ts": str(time.time()),
        "agent_channel": agent_cid,
    })
    await ari.bridge_add(h["bridge"], agent_cid)
    await ari.moh_stop(h["bridge"])
    await db.run("UPDATE calls SET status='answered', answered_at=now() WHERE call_key=%s",
                 (key,))
    ari.answered.setdefault(key, asyncio.Event()).set()
    await broker.publish({"type": "call_answered", "key": key,
                          "queue": h.get("queue"), "agent_id": h.get("agent_id")})
    log.info("call %s bridged (agent channel=%s)", key, agent_cid)


async def handle_stasis_end(ch: dict):
    cid = ch["id"]
    if cid in ari.agent_legs:
        key, agent_id = ari.agent_legs.pop(cid)
        h = await redis.hgetall(k_call(key)) if await redis.exists(k_call(key)) else {}
        if h and h.get("status") == "ringing":
            # agent's phone rang out or they rejected: put the call back
            await redis.hset(k_call(key), mapping={"status": "queued"})
            await redis.hdel(k_call(key), "agent_id", "agent_channel")
            await set_agent_state(agent_id, "available")
            await broker.publish({"type": "call_requeued", "key": key,
                                  "queue": h.get("queue")})
        # if status was 'answered', the agent simply ended the conversation:
        # hang up the caller, then the caller's StasisEnd finalizes the call.
        if h and h.get("status") == "answered" and h.get("channel"):
            await ari.hangup(h["channel"])
        return

    ev = ari.hangups.get(cid)
    if ev is not None:
        ev.set()
    ari.dtmf.pop(cid, None)
