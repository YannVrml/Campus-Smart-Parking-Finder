# RPC API

## Framed Request

```json
{
  "rpcId": 1,
  "method": "getAvailability",
  "args": ["G1"],
  "clientId": "8a4d..."
}
```

## Framed Reply

```json
{
  "rpcId": 1,
  "result": 42,
  "error": null
}
```

## Methods

- `getLots() -> List<Lot>`
- `getAvailability(lotId) -> int`
- `reserve(lotId, plate) -> bool` (`false` with error `FULL` or `EXISTS`)
- `cancel(lotId, plate) -> bool` (`false` with error `NOT_FOUND`)
- `subscribe(lotId) -> subId`
- `unsubscribe(subId) -> bool`

## Event Channel

Client opens a separate TCP connection to event port and sends framed hello:

```json
{ "type": "event_hello", "clientId": "8a4d..." }
```

Server ack:

```json
{ "ok": true, "clientId": "8a4d..." }
```

Event payload:

```json
{ "type": "EVENT", "lotId": "G1", "free": 55, "timestamp": "2026-03-03T...Z" }
```
