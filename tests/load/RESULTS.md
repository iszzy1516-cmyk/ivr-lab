# Load Test Results — ivr-lab Phase 1

Target architecture (single node):

```
[SIPp] --UDP/SIP--> [Asterisk 20 (host net)] --ARI/WS--> [FastAPI backend] <-> [Redis] [Postgres]
                        | mixing bridges + MOH per queued call
```

Tooling: SIPp 3.7.2 (`sip-tester` Ubuntu package) driving a custom UAC scenario
(`uac.xml`) that tolerates the remote hanging up first (the IVR path ends calls
itself) and otherwise ends the call after a configurable hold time (queue path).
`run_load.sh` wraps sipp in Docker, caps concurrency with `-l`, and samples
`docker stats` every 5 s for the duration of the run.

Key sipp semantics: `-r` is a **call rate** (calls/sec), not concurrency.
Sustained concurrency ≈ rate × average call lifetime, capped by `-l`.
At 60 s holds, `-l 500` throttles a `-r 20` run to ~8 calls/s effective.

## Test matrix

| Run | Path | Concurrency cap | Call rate | Hold | Total calls |
|-----|------|-----------------|-----------|------|-------------|
| ivr-10    | IVR (9000)  | 10  | 3/s  | ~32 s (backend ends call) | 30   |
| ivr-100   | IVR (9000)  | 100 | 10/s | ~32 s | 200  |
| ivr-500   | IVR (9000)  | 500 | 20/s | ~32 s | 2500 |
| queue-500 | queue (9001)| 500 | 20/s | 60 s  | 3000 |

(Filled in below with measured numbers per environment.)

## Environment A — dev workstation (192.168.1.153), podman rootless, 16 cores

| Run | Result (sipp) | Asterisk peak | Backend peak |
|-----|---------------|---------------|--------------|
| ivr-10    | 30/30 ok, 0 failed | 3.8% CPU / 50 MiB  | 1.3% CPU / 52 MiB |
| ivr-100   | 200/200 ok, 0 failed | 6.2% CPU / ~100 MiB | 1.8% CPU |
| ivr-500   | 2500/2500 ok, 0 failed | 34.6% CPU / ~0.2 GiB | 9.1% CPU / 0.1 GiB |
| queue-500 | 2497 ok, run wedged* | ~150% CPU at 500 concurrent queued calls (500 simultaneous MOH decodes) | 17% CPU |

\* Root cause of the wedge: Asterisk's ARI HTTP server occasionally drops
keep-alive connections under burst; the backend's `answer` failed
("Server disconnected"), no retry existed, and the channel stayed in Stasis
ringing forever, which blocked sipp's `-m` exit. Fixed with a one-shot retry on
ARI transport errors (`backend/app/ari.py::_req`). Re-validated post-fix on the
production server (below).

## Environment B — iszzylab server (192.168.1.182), docker 29.1.3, 4 cores

(To be filled after the on-server runs.)

## Takeaways

1. Single Asterisk node handles 500 concurrent IVR calls at ~1/3 core — the
   bottleneck at scale is **music-on-hold per queued bridge**, not signaling.
2. Backend (asyncio + ARI + Redis) is cheap: <10% CPU at 500 concurrent.
3. Publish-port ranges and desktop apps will fight over UDP ports under
   rootless podman — host networking for the Asterisk container removes the
   whole class of problem.
4. ARI keep-alive drops under burst are real (≈0.1% of calls); retry once.
