import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ServerConfig:
    host: str
    command_port: int
    rpc_port: int
    sensor_port: int
    event_port: int
    reservation_ttl_seconds: int
    sensor_worker_threads: int
    sensor_queue_size: int
    event_queue_size: int
    lots: list[dict[str, Any]]


DEFAULT_CONFIG_PATH = "config.json"


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> ServerConfig:
    with open(path, "r", encoding="utf-8") as file:
        raw = json.load(file)

    return ServerConfig(
        host=raw.get("host", "127.0.0.1"),
        command_port=int(raw.get("command_port", 9000)),
        rpc_port=int(raw.get("rpc_port", 9001)),
        sensor_port=int(raw.get("sensor_port", 9002)),
        event_port=int(raw.get("event_port", 9003)),
        reservation_ttl_seconds=int(raw.get("reservation_ttl_seconds", 300)),
        sensor_worker_threads=int(raw.get("sensor_worker_threads", 2)),
        sensor_queue_size=int(raw.get("sensor_queue_size", 2048)),
        event_queue_size=int(raw.get("event_queue_size", 256)),
        lots=raw.get("lots", [{"id": "LotA", "capacity": 50}, {"id": "LotB", "capacity": 80}]),
    )
