import argparse
import random
import socket
import time


def run_sensor(host: str, port: int, lots: list[str], updates_per_second: float) -> None:
    interval = 1.0 / updates_per_second
    with socket.create_connection((host, port)) as sock:
        while True:
            lot_id = random.choice(lots)
            delta = random.choice([-1, 1])
            line = f"UPDATE {lot_id} {delta}\n"
            sock.sendall(line.encode("utf-8"))
            time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sensor simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--sensor-port", type=int, default=9002)
    parser.add_argument("--lots", nargs="+", required=True)
    parser.add_argument("--ups", type=float, default=10.0, help="Updates per second")
    args = parser.parse_args()

    run_sensor(args.host, args.sensor_port, args.lots, args.ups)


if __name__ == "__main__":
    main()
