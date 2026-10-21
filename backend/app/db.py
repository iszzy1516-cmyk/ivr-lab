"""Tiny psycopg v3 wrapper (sync calls offloaded to a thread)."""
import asyncio
import os

import psycopg

DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://contactcenter:contactcenter@postgres:5432/contactcenter",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    extension   TEXT UNIQUE NOT NULL,
    state       TEXT NOT NULL DEFAULT 'offline',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calls (
    id              BIGSERIAL PRIMARY KEY,
    call_key        TEXT UNIQUE NOT NULL,
    channel_id      TEXT,
    caller_id       TEXT,
    queue           TEXT,
    status          TEXT,
    outcome         TEXT,
    enqueued_at     TIMESTAMPTZ,
    answered_at     TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    wait_seconds    DOUBLE PRECISION,
    handle_seconds  DOUBLE PRECISION
);
"""


def _init() -> None:
    with psycopg.connect(DSN) as conn:
        conn.execute(_SCHEMA)
        conn.commit()


def _run(sql, params=(), fetch=None):
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        conn.commit()


def init() -> None:
    _init()


async def run(sql, params=(), fetch=None):
    return await asyncio.to_thread(_run, sql, params, fetch)
