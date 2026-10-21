# IVR Lab — Zero-Cost Self-Hosted Contact Center (Phase 1 MVP)

Incoming calls hit an Asterisk IVR, queue in Redis, get picked up by agents from a
live web dashboard, and land in Postgres with Prometheus/Grafana metrics.
Everything runs locally in Docker — no paid APIs, no cloud.

```
[Softphone]  SIP/RTP (UDP 5060 / 10000-20000)
     |
     v
[Asterisk 20] -- Stasis() --> [FastAPI backend] --ARI--> controls calls (play, DTMF, bridge)
     |                                |        \
  sounds/MOH                   [Redis: queues,    [PostgreSQL: call log,
     |                          agent states]       agents]
     v                                |
[Agent softphone] <-------- [WebSocket /events] --> [Agent dashboard :8000]

[Prometheus :9090] scrapes backend /metrics -> [Grafana :3000]
```

## Quick start

The stack deploys on the lab server **iszzylab (192.168.1.182)** — a headless
Ubuntu 24.04 box with Docker. From any workstation with the project:

```bash
rsync -az --exclude 'tests/load/results' ivr_lab/ iszzy@192.168.1.182:~/ivr_lab/
ssh iszzy@192.168.1.182 'cd ~/ivr_lab && docker compose up -d --build'
ssh iszzy@192.168.1.182 'cd ~/ivr_lab && docker compose ps'   # wait for healthy
```

Open **http://192.168.1.182:8000** — the agent dashboard.

Notes on this topology:
- Asterisk runs with **host networking** on the server: SIP/RTP bind
  `wlp58s0` directly, no Docker port-forwarding in the media path. ARI :8088 is
  therefore reachable on the server LAN interface (lab-grade creds).
- The backend reaches ARI via `host.docker.internal` (`host-gateway`).
- Load tests (SIPp) can run from any machine on the LAN:
  `tests/load/run_load.sh` targets `192.168.1.182` by default.

## Demo script

1. **Register softphones** (Zoiper / Linphone) on your laptop/phone:

   | Account | Setting |
   |---|---|
   | Domain / Proxy | `192.168.1.182` (UDP, port 5060) |
   | User / Auth | `6001` / `pass6001` (Alice), `6002` / `pass6002` (Bob) |

2. On the dashboard, pick an agent and click **Go Available**.
3. From the other softphone, **dial 9000**. You hear the IVR:
   "…press 1 for sales, press 2 for support."
4. Press **1** or **2**. The call appears in the queue table with a live wait timer.
5. Click **Answer** in the dashboard → the agent's softphone rings → answer it →
   you're bridged. Either party hanging up ends the call.
6. Check **Call history** on the dashboard, or the Grafana dashboard
   (http://192.168.1.182:3000, admin/admin) for call rate / wait / handle time.

Agents can also call each other directly (6001 ↔ 6002) to test audio both ways.

## Layout

```
docker-compose.yml          all services (restart: always — survives reboot)
asterisk/
  Dockerfile                andrius/asterisk:20 + core sounds + MOH
  config/pjsip.conf         transport + softphone endpoints 6001/6002
  config/extensions.conf    9000 -> Stasis(ivr); _6XXX direct agent dialing
  config/ari.conf           ARI user (ariuser/aripass — lab only)
  config/http.conf          ARI on :8088
backend/app/
  ari.py                    ARI client + IVR/queue/bridge state machine
  main.py                   REST API + WebSocket + static dashboard
  core.py                   Redis, metrics, event broker
  db.py                     Postgres schema + tiny query helper
  static/index.html         agent dashboard (no build step)
prometheus/prometheus.yml   scrape config
grafana/provisioning/       datasource + prebuilt dashboard
```

## API cheat sheet

| Endpoint | What it does |
|---|---|
| `GET /queue` | calls currently waiting per queue |
| `GET /agents` | agents and their state |
| `POST /agents` | register agent `{name, extension}` |
| `POST /agents/{id}/status` | `{state: available\|offline}` (`busy` is system-managed) |
| `POST /queue/{queue}/{key}/answer` | pick up a queued call `{agent_id}` |
| `GET /calls?limit=50` | call history |
| `WS /events` | live `call_queued / call_ringing / call_answered / call_ended / agent_update` |
| `GET /metrics` | Prometheus format |

## How a call flows

1. `9000` matches `exten => 9000,1,Stasis(ivr,inbound)` — dialplan hands the
   channel to ARI app `ivr`.
2. The backend (`backend/app/ari.py`) answers, plays `sound:basic-pbx-ivr-main`,
   and waits up to 10 s per attempt for DTMF (two attempts).
3. Choice 1/2 → create a mixing bridge, park the caller there with music on
   hold, `RPUSH` into `cc:queue:{sales|support}`, insert a `queued` row in
   Postgres, broadcast `call_queued` over WebSocket.
4. Dashboard `Answer` → backend sets agent `busy`, originates
   `PJSIP/{extension}` with `appArgs=agent,{call_key}`; agent's softphone rings.
5. Agent answers → `StasisStart(agent)` → agent channel joins the bridge → MOH
   stops → caller and agent talk. Postgres row gets `answered_at`.
6. Either leg hangs up → other leg is hung up, row finalized with
   `outcome` (`answered` / `abandoned` / `no_choice`), `wait_seconds`,
   `handle_seconds`; Prometheus counters/histograms tick; agent returns to
   `available`.

## Ops notes

- **Survives reboot**: every service has `restart: always`. Docker itself starts
  on boot by default on Ubuntu (`systemctl is-enabled docker`).
- **Change the external IP** (if your server IP changes):
  `SIP_EXTERNAL_IP=192.168.x.x docker compose up -d asterisk` — or edit the
  default in `docker-compose.yml`. The entrypoint rewrites `pjsip.conf`.
- **Edit Asterisk config** → files are bind-mounted: `docker compose restart asterisk`.
  Asterisk config is *not* hot-reloaded (no `module reload` wiring) — restart is fine at this scale.
- **Firewall**: if ufw is active: `sudo ufw allow 5060/udp && sudo ufw allow 10000:10100/udp`.
- **ARI credentials are lab-grade** (`aripass`, `contactcenter` DB password).
  Don't expose these ports beyond your LAN.
- **Debug**:
  - `docker logs cc-backend` — backend call-control log (every DTMF is logged)
  - `docker exec asterisk asterisk -rx "core show verbose"` / `"pjsip show endpoints"`
  - `curl -u ariuser:aripass http://localhost:8088/ari/endpoints`
  - `docker exec cc-postgres psql -U contactcenter contactcenter -c "table calls"`
- **Build quirk on this host**: BuildKit can't reach Docker Hub over IPv6 here;
  use the classic builder when rebuilding: `DOCKER_BUILDKIT=0 docker compose build`.

## Test a call without a softphone (headless smoke test)

The dialplan has three test hooks for headless verification (run from any
machine on the LAN; the examples assume you run them on the server itself):

- `9001` / `9002` — skip the IVR and go straight to the sales/support queue
  (also handy to demo the queue without listening to the prompt)
- `park` — a 5-minute Wait() extension used as the far leg of test calls
- `agent-auto` — an auto-answering endpoint used as a fake agent leg

```bash
AUTH="ariuser:aripass"; BASE="http://localhost:8088/ari"

# 1. inbound caller: near leg parks, far leg runs 9001 -> straight to sales queue
curl -s -u $AUTH -X POST "$BASE/channels" -H 'Content-Type: application/json' \
  -d '{"endpoint":"Local/9001@default","extension":"park","context":"default","priority":"1"}'

curl -s localhost:8000/queue    # -> {"sales":[{"key":"...","status":"queued",...}],...}

# 2. "agent" answers (fake auto-answer leg -> bridges on answer)
KEY=$(curl -s localhost:8000/queue | python3 -c 'import sys,json;print(json.load(sys.stdin)["sales"][0]["key"])')
curl -s -u $AUTH -X POST "$BASE/channels" -H 'Content-Type: application/json' \
  -d "{\"endpoint\":\"Local/agent-auto@default\",\"app\":\"ivr\",\"appArgs\":\"agent,$KEY\"}"

# 3. hang up the caller (find its channel id via GET /channels, exten=9001)
#    -> backend finalizes: outcome=answered, wait_seconds, handle_seconds in Postgres
```

A pure-IVR path test (prompt -> no DTMF -> goodbye -> `no_choice` row):

```bash
curl -s -u $AUTH -X POST "$BASE/channels" -H 'Content-Type: application/json' \
  -d '{"endpoint":"Local/9000@default","extension":"park","context":"default","priority":"1"}'
sleep 30   # two 10s DTMF timeouts + prompt/goodbye
```

Note: DTMF can't be injected through Local channels, so the 1/2 menu selection
itself needs a real softphone — that's the one path only `9000` + Zoiper covers.

## Known MVP warts (fix in later phases)

- IVR prompt is the stock Asterisk demo greeting ("press 1 for sales / 2 for
  support" — matches our routing, but it's clearly a stock voice).
- Agents are offered calls manually from the dashboard; no automatic
  offer/longest-idle routing yet.
- One redis list per queue, no priority/skill routing.
- No call recording, CDR UI, authentication on the dashboard, or TLS for SIP.
