import argparse
import socket
import threading
import time
import uuid
from typing import Any

from framing import recv_framed, send_framed


class TimeoutError(Exception):
    pass


class ParkingRpcClient:
    def __init__(self, host: str, rpc_port: int, timeout_seconds: float = 2.0):
        self.host = host
        self.rpc_port = rpc_port
        self.timeout_seconds = timeout_seconds
        self.client_id = str(uuid.uuid4())
        self._rpc_id = 0
        self._lock = threading.Lock()

        self.sock = socket.create_connection((self.host, self.rpc_port), timeout=self.timeout_seconds)
        self.sock.settimeout(self.timeout_seconds)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _call(self, method: str, args: list[Any]) -> Any:
        with self._lock:
            self._rpc_id += 1
            rpc_id = self._rpc_id
            request = {
                "rpcId": rpc_id,
                "method": method,
                "args": args,
                "clientId": self.client_id,
            }

            send_framed(self.sock, request)
            start = time.perf_counter()

            while True:
                remaining = self.timeout_seconds - (time.perf_counter() - start)
                if remaining <= 0:
                    raise TimeoutError(f"RPC timeout for method={method}")

                self.sock.settimeout(remaining)
                try:
                    reply = recv_framed(self.sock)
                except socket.timeout as exc:
                    raise TimeoutError(f"RPC timeout for method={method}") from exc

                if int(reply.get("rpcId", -1)) != rpc_id:
                    continue

                error = reply.get("error")
                if error:
                    raise RuntimeError(str(error))
                return reply.get("result")

    def get_lots(self) -> list[dict[str, Any]]:
        return self._call("getLots", [])

    def get_availability(self, lot_id: str) -> int:
        return int(self._call("getAvailability", [lot_id]))

    def reserve(self, lot_id: str, plate: str) -> bool:
        return bool(self._call("reserve", [lot_id, plate]))

    def cancel(self, lot_id: str, plate: str) -> bool:
        return bool(self._call("cancel", [lot_id, plate]))

    def subscribe(self, lot_id: str) -> str:
        return str(self._call("subscribe", [lot_id]))

    def unsubscribe(self, sub_id: str) -> bool:
        return bool(self._call("unsubscribe", [sub_id]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Parking RPC client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--rpc-port", type=int, default=9001)
    parser.add_argument("--timeout", type=float, default=2.0)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("lots")

    avail_parser = subparsers.add_parser("avail")
    avail_parser.add_argument("lot_id")

    reserve_parser = subparsers.add_parser("reserve")
    reserve_parser.add_argument("lot_id")
    reserve_parser.add_argument("plate")

    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("lot_id")
    cancel_parser.add_argument("plate")

    args = parser.parse_args()

    client = ParkingRpcClient(args.host, args.rpc_port, args.timeout)
    try:
        if args.command == "lots":
            print(client.get_lots())
        elif args.command == "avail":
            print(client.get_availability(args.lot_id))
        elif args.command == "reserve":
            print(client.reserve(args.lot_id, args.plate))
        elif args.command == "cancel":
            print(client.cancel(args.lot_id, args.plate))
    finally:
        client.close()


if __name__ == "__main__":
    main()
