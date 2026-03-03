import argparse
import socket
import threading
import uuid

from framing import recv_framed, send_framed
from rpc_client import ParkingRpcClient


class SubscriberClient:
    def __init__(self, host: str, rpc_port: int, event_port: int, timeout_seconds: float):
        self.client_id = str(uuid.uuid4())
        self.host = host
        self.event_port = event_port
        self.event_sock = socket.create_connection((host, event_port), timeout=timeout_seconds)

        send_framed(self.event_sock, {"type": "event_hello", "clientId": self.client_id})
        ack = recv_framed(self.event_sock)
        if not ack.get("ok"):
            raise RuntimeError(f"event channel failed: {ack.get('error')}")

        self.rpc = ParkingRpcClient(host, rpc_port, timeout_seconds)
        self.rpc.client_id = self.client_id
        self.sub_ids: list[str] = []
        self._stop = threading.Event()

    def subscribe(self, lot_id: str) -> str:
        sub_id = self.rpc.subscribe(lot_id)
        self.sub_ids.append(sub_id)
        return sub_id

    def unsubscribe_all(self) -> None:
        for sub_id in list(self.sub_ids):
            try:
                self.rpc.unsubscribe(sub_id)
            except RuntimeError:
                pass
            self.sub_ids.remove(sub_id)

    def listen(self) -> None:
        while not self._stop.is_set():
            event = recv_framed(self.event_sock)
            if event.get("type") == "EVENT":
                print(f"EVENT {event['lotId']} {event['free']} {event['timestamp']}")

    def close(self) -> None:
        self._stop.set()
        self.unsubscribe_all()
        self.rpc.close()
        try:
            self.event_sock.close()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Subscriber client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--rpc-port", type=int, default=9001)
    parser.add_argument("--event-port", type=int, default=9003)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--lot", action="append", required=True, help="Lot ID to subscribe to (repeatable)")
    args = parser.parse_args()

    client = SubscriberClient(args.host, args.rpc_port, args.event_port, args.timeout)
    try:
        for lot in args.lot:
            sub_id = client.subscribe(lot)
            print(f"Subscribed lot={lot} subId={sub_id}")
        client.listen()
    finally:
        client.close()


if __name__ == "__main__":
    main()
