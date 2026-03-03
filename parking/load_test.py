import argparse
import statistics
import threading
import time
from collections import deque

from rpc_client import ParkingRpcClient, TimeoutError


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(int(len(values) * p), len(values) - 1)
    return values[idx]


def run_load(host: str, port: int, workers: int, duration: float, op: str, lot_id: str) -> None:
    latencies = deque()
    total = 0
    lock = threading.Lock()
    stop_at = time.perf_counter() + duration

    def worker(index: int) -> None:
        nonlocal total
        client = ParkingRpcClient(host, port, timeout_seconds=2.0)
        try:
            while time.perf_counter() < stop_at:
                plate = f"PLT-{index}-{time.time_ns() % 100000}"
                start = time.perf_counter()
                try:
                    if op == "avail":
                        client.get_availability(lot_id)
                    elif op == "reserve":
                        ok = client.reserve(lot_id, plate)
                        if ok:
                            client.cancel(lot_id, plate)
                except (TimeoutError, RuntimeError):
                    continue
                latency = (time.perf_counter() - start) * 1000
                with lock:
                    total += 1
                    latencies.append(latency)
        finally:
            client.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lat_list = list(latencies)
    throughput = total / duration
    med = statistics.median(lat_list) if lat_list else 0.0
    p95 = percentile(lat_list, 0.95)

    print(f"workers={workers} op={op} duration={duration}s")
    print(f"throughput_req_per_s={throughput:.2f}")
    print(f"latency_ms_median={med:.2f}")
    print(f"latency_ms_p95={p95:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple RPC load test")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--rpc-port", type=int, default=9001)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--op", choices=["avail", "reserve"], required=True)
    parser.add_argument("--lot", required=True)
    args = parser.parse_args()

    run_load(args.host, args.rpc_port, args.workers, args.duration, args.op, args.lot)


if __name__ == "__main__":
    main()
