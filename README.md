# Campus Smart Parking Finder

Python implementation of a multithreaded parking server with:

- text command protocol (`LOTS`, `AVAIL`, `RESERVE`, `CANCEL`, `PING`)
- minimal RPC over TCP with length-prefixed framing
- asynchronous sensor updates
- publish/subscribe notifications with back-pressure policy

## Environment Setup (venv)

Create venv:

```bash
python -m venv .venv
```

Activate:

- macOS/Linux:

```bash
source .venv/bin/activate
```

- Windows:

```powershell
.venv\Scripts\activate
```

Install:

```bash
pip install -r requirements.txt
```

This project is **stdlib only**.

## Run

Start server:

```bash
python main.py --config config.json
```

Run text protocol manually (example):

```bash
nc 127.0.0.1 9000
```

Run RPC client examples:

```bash
python parking/rpc_client.py --rpc-port 9001 lots
python parking/rpc_client.py --rpc-port 9001 avail G1
python parking/rpc_client.py --rpc-port 9001 reserve G1 ABC123
python parking/rpc_client.py --rpc-port 9001 cancel G1 ABC123
```

Run subscriber client:

```bash
python parking/subscriber_client.py --rpc-port 9001 --event-port 9003 --lot G1
```

Run sensor simulator (10 updates/sec default):

```bash
python parking/sensor_sim.py --sensor-port 9002 --lots G1 G2 G3 --ups 10
```

## Thread Model and Server Organization

- **Thread-per-connection** for command, RPC, sensor, and event listeners.
- **Dedicated sensor worker pool** (`sensor_worker_threads`) processes queued updates from sensors.
- **Dedicated sender thread per event connection** pushes pub/sub notifications.
- Reservation expiration runs in a background cleaner thread every 1 second.

Rationale: connection handlers are simple and isolated; asynchronous update and notifier paths avoid blocking RPC request/response handling.

## Text Protocol (newline-delimited UTF-8)

- `LOTS` -> JSON list of lots: `{id, capacity, occupied, free}`
- `AVAIL <lotId>` -> free spots as integer
- `RESERVE <lotId> <plate>` -> `OK | FULL | EXISTS`
- `CANCEL <lotId> <plate>` -> `OK | NOT_FOUND`
- `PING` -> `PONG`

## RPC Framing / Marshalling Spec

- Framing: **4-byte length prefix**, unsigned big-endian (`!I`), followed by UTF-8 JSON payload.
- Request shape:

```json
{ "rpcId": 1, "method": "reserve", "args": ["G1", "ABC123"], "clientId": "..." }
```

- Reply shape:

```json
{ "rpcId": 1, "result": true, "error": null }
```

### Parameter Passing Note

- Endianness: big-endian for frame length prefix.
- Types: JSON native types (`number`, `string`, `array`, `object`, `null`, `bool`).
- IDs: `rpcId` is serialized as JSON number and interpreted as unsigned logical identifier.

RPC path:
`Caller -> Client Stub -> TCP -> Server Skeleton -> Method -> Return -> Client Stub -> Caller`

## Timeout Policy

`ParkingRpcClient` enforces per-RPC timeout (`--timeout`, default `2.0s`) and raises `TimeoutError` on deadline expiration.

## Async Messaging + Pub/Sub Design

- Sensors connect to `sensor_port` and send `UPDATE <lotId> <delta>`.
- Updates are queued (`sensor_queue_size`) and applied by sensor workers.
- Subscribers use a **separate TCP event connection** (`event_port`) and RPC `subscribe(lotId)` / `unsubscribe(subId)`.
- On any free-space change (`RESERVE`, `CANCEL`, `UPDATE`, reservation expiry), server publishes:
  - `{"type":"EVENT","lotId":"...","free":N,"timestamp":"..."}`

## Back-Pressure Policy

- Sensor ingress: bounded queue; overflow causes clear rejection in logs (`sensor_queue_reject`).
- Subscriber egress: bounded per-subscriber queue (`event_queue_size`), **drop oldest** on overflow, then enqueue newest event.
- This preserves fresh occupancy data while preventing slow subscribers from blocking RPC operations.

## Logging

Structured JSON logs include event type, lotId, plate (when relevant), and timestamp.

## Evaluation Commands

Sync RPC baseline (30s each):

```bash
python parking/load_test.py --workers 1 --duration 30 --op avail --lot G1
python parking/load_test.py --workers 4 --duration 30 --op avail --lot G1
python parking/load_test.py --workers 8 --duration 30 --op avail --lot G1
python parking/load_test.py --workers 16 --duration 30 --op avail --lot G1
```

Repeat with `--op reserve`.

Async + pub/sub stress example:

1. Start server
2. Start one or more sensors

```bash
python parking/sensor_sim.py --lots G1 G2 G3 --ups 10
```

3. Start one or more subscribers
4. Re-run load tests and compare throughput/latency.

## Requirement -> Implementation Map

- **A. Multithreaded Parking Server**
  - Concurrent TCP handling: `parking/server.py` (`_serve_text`, `_serve_rpc`, `_handle_*` threads)
  - Text protocol (`LOTS`, `AVAIL`, `RESERVE`, `CANCEL`, `PING`): `parking/server.py` (`_process_text_command`)
  - Shared state + synchronization + no overbooking: `parking/server.py` (`ParkingState`, lock-protected `reserve`/`cancel`)

- **B. Minimal RPC Layer**
  - Length-prefixed framing: `parking/framing.py` (`send_framed`, `recv_framed`, `!I` big-endian)
  - RPC request/reply schema: `parking/server.py` (`_process_rpc_request`), `API.md`
  - Client stub + timeout + `TimeoutError`: `parking/rpc_client.py`
  - Caller -> Stub -> TCP -> Skeleton path documented: this README (RPC section)

- **C. Asynchronous Messaging Path**
  - Sensor port + `UPDATE <lotId> <delta>` ingestion: `parking/server.py` (`_serve_sensor`, `_handle_sensor_client`)
  - Enqueue + worker application: `parking/server.py` (`sensor_queue`, `_sensor_worker`)
  - Non-blocking vs RPC flow: separate event connection + dedicated sender thread per subscriber endpoint

- **D. Publish/Subscribe**
  - RPC methods `subscribe(lotId)` / `unsubscribe(subId)`: `parking/server.py`, `parking/rpc_client.py`
  - Event publish on free change: `parking/server.py` (`_publish_lot_change`) and payload `EVENT lotId free timestamp`
  - Back-pressure policy: bounded per-subscriber queue, drop oldest (`EventEndpoint.enqueue`)

- **Non-functional + deliverables**
  - Reservation TTL configurable (default 300s): `config.json`, `parking/config.py`, `parking/server.py`
  - Structured logs (event, lotId, plate, timestamp): `parking/server.py` (`log_event`)
  - Configurable ports/threads/lots: `config.json`
  - venv + requirements + run commands: this README, `requirements.txt` (stdlib only)

## Reliability Notes

- Sensor disconnects: sensor client thread exits; server continues processing other clients.
- Idempotency: `RESERVE` returns `EXISTS` for duplicate active reservation; `CANCEL` returns `NOT_FOUND` when absent.
- Reservation expiration: automatic TTL-based release (`reservation_ttl_seconds`, default 300).
