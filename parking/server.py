import argparse
import json
import logging
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from config import ServerConfig, load_config
from framing import FramingError, recv_framed, send_framed


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("parking")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_ts() -> str:
    return now_utc().isoformat()


def log_event(event_type: str, **kwargs: Any) -> None:
    payload = {"event": event_type, "timestamp": utc_ts(), **kwargs}
    logger.info(json.dumps(payload, separators=(",", ":")))


@dataclass
class LotState:
    lot_id: str
    capacity: int
    occupied: int = 0
    reservations: dict[str, datetime] = field(default_factory=dict)

    @property
    def free(self) -> int:
        return max(self.capacity - self.occupied, 0)


@dataclass
class Subscription:
    sub_id: str
    lot_id: str
    client_id: str


class EventEndpoint:
    def __init__(self, client_id: str, conn: socket.socket, queue_size: int):
        self.client_id = client_id
        self.conn = conn
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_size)
        self.closed = threading.Event()
        self.sender_thread = threading.Thread(target=self._sender_loop, name=f"event-sender-{client_id}", daemon=True)

    def start(self) -> None:
        self.sender_thread.start()

    def enqueue(self, event: dict[str, Any]) -> bool:
        if self.closed.is_set():
            return False
        try:
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                return False
            try:
                self.queue.put_nowait(event)
                log_event("event_drop_oldest", client_id=self.client_id)
                return True
            except queue.Full:
                return False

    def close(self) -> None:
        self.closed.set()
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def _sender_loop(self) -> None:
        while not self.closed.is_set():
            try:
                message = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                send_framed(self.conn, message)
            except OSError:
                break
        self.closed.set()


class ParkingState:
    def __init__(self, lots: list[dict[str, Any]], reservation_ttl_seconds: int):
        self._lock = threading.RLock()
        self._lots: dict[str, LotState] = {
            entry["id"]: LotState(lot_id=entry["id"], capacity=int(entry["capacity"])) for entry in lots
        }
        self._reservation_ttl = timedelta(seconds=reservation_ttl_seconds)

    def list_lots(self) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_locked()
            return [
                {
                    "id": lot.lot_id,
                    "capacity": lot.capacity,
                    "occupied": lot.occupied,
                    "free": lot.free,
                }
                for lot in self._lots.values()
            ]

    def get_free(self, lot_id: str) -> int:
        with self._lock:
            self._expire_locked()
            lot = self._require_lot(lot_id)
            return lot.free

    def reserve(self, lot_id: str, plate: str) -> tuple[str, int | None]:
        with self._lock:
            self._expire_locked()
            lot = self._require_lot(lot_id)
            if plate in lot.reservations:
                return "EXISTS", None
            if lot.occupied >= lot.capacity:
                return "FULL", None
            lot.occupied += 1
            lot.reservations[plate] = now_utc() + self._reservation_ttl
            return "OK", lot.free

    def cancel(self, lot_id: str, plate: str) -> tuple[str, int | None]:
        with self._lock:
            self._expire_locked()
            lot = self._require_lot(lot_id)
            if plate not in lot.reservations:
                return "NOT_FOUND", None
            del lot.reservations[plate]
            lot.occupied = max(lot.occupied - 1, 0)
            return "OK", lot.free

    def apply_update(self, lot_id: str, delta: int) -> int:
        with self._lock:
            self._expire_locked()
            lot = self._require_lot(lot_id)
            lot.occupied = min(max(lot.occupied + delta, 0), lot.capacity)
            return lot.free

    def expire_reservations(self) -> list[tuple[str, str, int]]:
        with self._lock:
            return self._expire_locked()

    def _expire_locked(self) -> list[tuple[str, str, int]]:
        expired: list[tuple[str, str, int]] = []
        current = now_utc()
        for lot in self._lots.values():
            stale = [plate for plate, deadline in lot.reservations.items() if deadline <= current]
            for plate in stale:
                del lot.reservations[plate]
                lot.occupied = max(lot.occupied - 1, 0)
                expired.append((lot.lot_id, plate, lot.free))
        return expired

    def _require_lot(self, lot_id: str) -> LotState:
        lot = self._lots.get(lot_id)
        if not lot:
            raise KeyError(f"unknown lot: {lot_id}")
        return lot


class ParkingServer:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.state = ParkingState(config.lots, config.reservation_ttl_seconds)
        self.running = threading.Event()
        self.running.set()

        self.sensor_queue: queue.Queue[tuple[str, int]] = queue.Queue(maxsize=config.sensor_queue_size)

        self.subscriptions_lock = threading.RLock()
        self.subscriptions: dict[str, Subscription] = {}
        self.subs_by_lot: dict[str, set[str]] = {}
        self.event_endpoints: dict[str, EventEndpoint] = {}

    def start(self) -> None:
        workers = [
            threading.Thread(target=self._sensor_worker, name=f"sensor-worker-{idx}", daemon=True)
            for idx in range(self.config.sensor_worker_threads)
        ]
        for worker in workers:
            worker.start()

        threading.Thread(target=self._reservation_cleaner, name="reservation-cleaner", daemon=True).start()

        listeners = [
            threading.Thread(target=self._serve_text, name="text-listener", daemon=True),
            threading.Thread(target=self._serve_rpc, name="rpc-listener", daemon=True),
            threading.Thread(target=self._serve_sensor, name="sensor-listener", daemon=True),
            threading.Thread(target=self._serve_events, name="event-listener", daemon=True),
        ]

        for listener in listeners:
            listener.start()

        log_event(
            "server_started",
            host=self.config.host,
            command_port=self.config.command_port,
            rpc_port=self.config.rpc_port,
            sensor_port=self.config.sensor_port,
            event_port=self.config.event_port,
        )

        try:
            while self.running.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self) -> None:
        self.running.clear()
        with self.subscriptions_lock:
            endpoints = list(self.event_endpoints.values())
            self.event_endpoints.clear()
        for endpoint in endpoints:
            endpoint.close()
        log_event("server_stopped")

    def _bind_socket(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.config.host, port))
        sock.listen(128)
        return sock

    def _serve_text(self) -> None:
        with self._bind_socket(self.config.command_port) as server_sock:
            while self.running.is_set():
                try:
                    conn, addr = server_sock.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle_text_client, args=(conn, addr), daemon=True).start()

    def _serve_rpc(self) -> None:
        with self._bind_socket(self.config.rpc_port) as server_sock:
            while self.running.is_set():
                try:
                    conn, addr = server_sock.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle_rpc_client, args=(conn, addr), daemon=True).start()

    def _serve_sensor(self) -> None:
        with self._bind_socket(self.config.sensor_port) as server_sock:
            while self.running.is_set():
                try:
                    conn, addr = server_sock.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle_sensor_client, args=(conn, addr), daemon=True).start()

    def _serve_events(self) -> None:
        with self._bind_socket(self.config.event_port) as server_sock:
            while self.running.is_set():
                try:
                    conn, addr = server_sock.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle_event_client, args=(conn, addr), daemon=True).start()

    def _handle_text_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        with conn:
            file = conn.makefile("rwb")
            while self.running.is_set():
                line = file.readline()
                if not line:
                    break
                response = self._process_text_command(line.decode("utf-8").strip())
                file.write((response + "\n").encode("utf-8"))
                file.flush()
        log_event("text_client_disconnected", remote=f"{addr[0]}:{addr[1]}")

    def _process_text_command(self, line: str) -> str:
        parts = line.split()
        if not parts:
            return "ERR EMPTY"

        cmd = parts[0].upper()
        try:
            if cmd == "LOTS":
                return json.dumps(self.state.list_lots(), separators=(",", ":"))
            if cmd == "AVAIL" and len(parts) == 2:
                return str(self.state.get_free(parts[1]))
            if cmd == "RESERVE" and len(parts) == 3:
                status, free = self.state.reserve(parts[1], parts[2])
                if status == "OK" and free is not None:
                    self._publish_lot_change(parts[1], free)
                    log_event("reserve", lotId=parts[1], plate=parts[2], status=status)
                return status
            if cmd == "CANCEL" and len(parts) == 3:
                status, free = self.state.cancel(parts[1], parts[2])
                if status == "OK" and free is not None:
                    self._publish_lot_change(parts[1], free)
                    log_event("cancel", lotId=parts[1], plate=parts[2], status=status)
                return status
            if cmd == "PING":
                return "PONG"
            return "ERR BAD_REQUEST"
        except KeyError as exc:
            return f"ERR {exc}"

    def _handle_rpc_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        with conn:
            while self.running.is_set():
                try:
                    request = recv_framed(conn)
                except EOFError:
                    break
                except (FramingError, OSError):
                    break
                reply = self._process_rpc_request(request)
                try:
                    send_framed(conn, reply)
                except OSError:
                    break
        log_event("rpc_client_disconnected", remote=f"{addr[0]}:{addr[1]}")

    def _process_rpc_request(self, request: dict[str, Any]) -> dict[str, Any]:
        rpc_id = int(request.get("rpcId", 0))
        method = request.get("method")
        args = request.get("args", [])
        client_id = request.get("clientId")

        try:
            if method == "getLots":
                result = self.state.list_lots()
                return {"rpcId": rpc_id, "result": result, "error": None}

            if method == "getAvailability":
                result = self.state.get_free(str(args[0]))
                return {"rpcId": rpc_id, "result": result, "error": None}

            if method == "reserve":
                status, free = self.state.reserve(str(args[0]), str(args[1]))
                if status == "OK" and free is not None:
                    self._publish_lot_change(str(args[0]), free)
                    log_event("reserve", lotId=str(args[0]), plate=str(args[1]), status=status)
                    return {"rpcId": rpc_id, "result": True, "error": None}
                return {"rpcId": rpc_id, "result": False, "error": status}

            if method == "cancel":
                status, free = self.state.cancel(str(args[0]), str(args[1]))
                if status == "OK" and free is not None:
                    self._publish_lot_change(str(args[0]), free)
                    log_event("cancel", lotId=str(args[0]), plate=str(args[1]), status=status)
                    return {"rpcId": rpc_id, "result": True, "error": None}
                return {"rpcId": rpc_id, "result": False, "error": status}

            if method == "subscribe":
                if not client_id:
                    return {"rpcId": rpc_id, "result": None, "error": "MISSING_CLIENT_ID"}
                try:
                    sub_id = self._subscribe(client_id=str(client_id), lot_id=str(args[0]))
                except ValueError as exc:
                    return {"rpcId": rpc_id, "result": None, "error": str(exc)}
                return {"rpcId": rpc_id, "result": sub_id, "error": None}

            if method == "unsubscribe":
                if not client_id:
                    return {"rpcId": rpc_id, "result": False, "error": "MISSING_CLIENT_ID"}
                ok = self._unsubscribe(client_id=str(client_id), sub_id=str(args[0]))
                return {"rpcId": rpc_id, "result": ok, "error": None}

            if method == "ping":
                return {"rpcId": rpc_id, "result": "PONG", "error": None}

            return {"rpcId": rpc_id, "result": None, "error": "UNKNOWN_METHOD"}
        except (IndexError, ValueError, TypeError):
            return {"rpcId": rpc_id, "result": None, "error": "BAD_ARGS"}
        except KeyError as exc:
            return {"rpcId": rpc_id, "result": None, "error": str(exc)}

    def _handle_sensor_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        with conn:
            file = conn.makefile("r", encoding="utf-8")
            for raw in file:
                line = raw.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) != 3 or parts[0].upper() != "UPDATE":
                    continue
                lot_id = parts[1]
                try:
                    delta = int(parts[2])
                except ValueError:
                    continue

                try:
                    self.sensor_queue.put_nowait((lot_id, delta))
                except queue.Full:
                    log_event("sensor_queue_reject", lotId=lot_id, delta=delta)
        log_event("sensor_disconnected", remote=f"{addr[0]}:{addr[1]}")

    def _sensor_worker(self) -> None:
        while self.running.is_set():
            try:
                lot_id, delta = self.sensor_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                free = self.state.apply_update(lot_id, delta)
                self._publish_lot_change(lot_id, free)
                log_event("sensor_update", lotId=lot_id, delta=delta, free=free)
            except KeyError:
                log_event("sensor_update_error", lotId=lot_id, delta=delta)

    def _reservation_cleaner(self) -> None:
        while self.running.is_set():
            expired = self.state.expire_reservations()
            for lot_id, plate, free in expired:
                self._publish_lot_change(lot_id, free)
                log_event("reservation_expired", lotId=lot_id, plate=plate)
            time.sleep(1.0)

    def _handle_event_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        endpoint: EventEndpoint | None = None
        try:
            hello = recv_framed(conn)
            if hello.get("type") != "event_hello" or not hello.get("clientId"):
                send_framed(conn, {"ok": False, "error": "BAD_HELLO"})
                conn.close()
                return

            client_id = str(hello["clientId"])
            endpoint = EventEndpoint(client_id=client_id, conn=conn, queue_size=self.config.event_queue_size)

            with self.subscriptions_lock:
                previous = self.event_endpoints.get(client_id)
                if previous:
                    previous.close()
                self.event_endpoints[client_id] = endpoint

            send_framed(conn, {"ok": True, "clientId": client_id})
            endpoint.start()
            while self.running.is_set() and not endpoint.closed.is_set():
                time.sleep(0.5)
        except (FramingError, EOFError, OSError):
            pass
        finally:
            if endpoint:
                endpoint.close()
                self._remove_endpoint(endpoint.client_id)
        log_event("event_client_disconnected", remote=f"{addr[0]}:{addr[1]}")

    def _remove_endpoint(self, client_id: str) -> None:
        with self.subscriptions_lock:
            endpoint = self.event_endpoints.get(client_id)
            if endpoint and endpoint.closed.is_set():
                self.event_endpoints.pop(client_id, None)

    def _subscribe(self, client_id: str, lot_id: str) -> str:
        self.state.get_free(lot_id)
        with self.subscriptions_lock:
            if client_id not in self.event_endpoints:
                raise ValueError("NO_EVENT_CHANNEL")
            sub_id = str(uuid.uuid4())
            sub = Subscription(sub_id=sub_id, lot_id=lot_id, client_id=client_id)
            self.subscriptions[sub_id] = sub
            self.subs_by_lot.setdefault(lot_id, set()).add(sub_id)
        log_event("subscribe", lotId=lot_id, subId=sub_id, clientId=client_id)
        return sub_id

    def _unsubscribe(self, client_id: str, sub_id: str) -> bool:
        with self.subscriptions_lock:
            sub = self.subscriptions.get(sub_id)
            if not sub or sub.client_id != client_id:
                return False
            self.subscriptions.pop(sub_id, None)
            lot_subs = self.subs_by_lot.get(sub.lot_id)
            if lot_subs:
                lot_subs.discard(sub_id)
                if not lot_subs:
                    self.subs_by_lot.pop(sub.lot_id, None)
        log_event("unsubscribe", subId=sub_id, clientId=client_id)
        return True

    def _publish_lot_change(self, lot_id: str, free: int) -> None:
        event = {"type": "EVENT", "lotId": lot_id, "free": free, "timestamp": utc_ts()}
        with self.subscriptions_lock:
            sub_ids = list(self.subs_by_lot.get(lot_id, set()))
            for sub_id in sub_ids:
                sub = self.subscriptions.get(sub_id)
                if not sub:
                    continue
                endpoint = self.event_endpoints.get(sub.client_id)
                if not endpoint:
                    continue
                endpoint.enqueue(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Campus Smart Parking Server")
    parser.add_argument("--config", default="config.json", help="Path to configuration JSON")
    args = parser.parse_args()

    config = load_config(args.config)
    server = ParkingServer(config)
    server.start()


if __name__ == "__main__":
    main()
