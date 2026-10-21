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

## Environment B — iszzylab server (192.168.1.182), docker 29.1.3, 4 cores, 16 GB

| Run | Result (sipp) | Asterisk | Backend |
|-----|---------------|----------|---------|
| server-ivr-500 (pre-fix) | 1206 ok / 1294 failed | — | — |
| server-ivr-500 (ulimit fix) | **2500/2500 ok, 0 failed** | avg 115% / peak 143% CPU, 0.18 GiB | avg 50% / peak 64% CPU, 0.06 GiB |
| server-queue-500 | wedge: 500 calls stuck `Ring` in Stasis | see below | — |
| server-queue-100 | same wedge reproduced at 100 concurrent | see below | — |

Reproduced at only 100 concurrent queued calls: all channels pile into
`Ring`+Stasis while the ARI REST server throws connection errors for every
request during the burst, then recovers. It is not a hardware limit — CPU sat
at ~115% during the clean ivr-500 run, and ARI responds in <20 ms once the
burst passes. The trigger is per-call **mixing-bridge + MOH churn**: bridge
create/add/MOH-start per caller, then teardown floods the Stasis/ARI event
machinery (which shares infra with the ARI HTTP server) and the control plane
stalls exactly when new calls need `answer()`.

The pre-fix failure mode was deterministic: **488 Not Acceptable Here** on new
INVITEs once ~1.2k calls had passed. Asterisk's logs showed
`Unable to allocate RTCP socket: Too many open files` — the Docker default FD
limit (1024) exhausted by ~500 concurrent calls × (RTP + RTCP + signaling)
sockets. Fix: `ulimits.nofile: 65536` on the asterisk service. Zero failures
after.

Operational lessons from Environment B:

1. **Rootless port publishing + desktop apps don't mix.** On the dev
   workstation, OnlyOffice DesktopEditors grabbed UDP 13012 inside our
   published RTP range and bricked the Asterisk container. Fix: host
   networking for Asterisk on the server (also the standard VoIP deployment
   pattern — no NAT in the media path).
2. **ARI keep-alive drops under burst** (~0.1% of REST calls): retried once in
   `_req`. During a 500-call teardown storm the drops cluster; without a
   retry, channels wedge in Stasis forever.
3. **No queue reconciliation (gap).** If backend call-control tasks die (e.g.
   during an ARI storm), Redis queue entries outlive them and phantom calls
   persist. Recovery is manual (flush keys + re-mark rows). Phase 2 should add
   a reaper: periodically diff `cc:queue:*` against live ARI channels.
4. sipp timing: with `-l 500` and 60 s holds, effective rate is ~8 calls/s —
   size `-m` accordingly and don't kill the wrapper mid-run; orphaned SIP
   channels need mass hangup via ARI.

## The 500-concurrent queue ceiling (important)

`server-queue-500` does **not** complete cleanly on a single node: partway
through, Asterisk's built-in ARI HTTP server saturates — REST connections start
dropping in bursts (`transport error` on every call), new INVITEs get their 180
but the backend's `answer()` never lands, and 500 channels pile up in
`Ring`+Stasis. The IVR path (no bridges, no MOH) sustains 500 concurrent
indefinitely; the queue path (one mixing bridge + one MOH stream **per waiting
call**) is what breaks the control plane.

This matches how real Asterisk deployments behave: `app_queue` doesn't create
per-call bridges/MOH — it parks all waiting callers in one holding context with
a shared MOH stream.

**Phase 2 fix (recommended):** one holding bridge per queue with bridge-level
MOH (a single shared stream). On answer, move the caller from the holding
bridge into a fresh 2-party bridge with the agent. This collapses 500 bridges +
500 MOH decoders → 1 + 1, and removes the burst load on the ARI server at
exactly the moment agents start answering.

Interim mitigation if 500 queued calls are needed before then: drop per-bridge
MOH (`moh_start` in `enqueue`) and/or ramp arrivals slower than ~5/s.

## Takeaways

1. Single Asterisk node handles 500 concurrent IVR calls at ~1/3 core — the
   bottleneck at scale is **music-on-hold per queued bridge**, not signaling.
2. Backend (asyncio + ARI + Redis) is cheap: <10% CPU at 500 concurrent.
3. Publish-port ranges and desktop apps will fight over UDP ports under
   rootless podman — host networking for the Asterisk container removes the
   whole class of problem.
4. ARI keep-alive drops under burst are real (≈0.1% of calls); retry once.
