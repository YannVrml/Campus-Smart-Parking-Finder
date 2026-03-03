import runpy
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).parent
    parking_dir = root / "parking"
    sys.path.insert(0, str(parking_dir))
    server_path = parking_dir / "server.py"
    runpy.run_path(str(server_path), run_name="__main__")


if __name__ == "__main__":
    main()