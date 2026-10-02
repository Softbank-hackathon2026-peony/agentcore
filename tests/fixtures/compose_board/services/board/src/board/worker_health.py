"""k8s exec liveness probe: 워커 루프가 30초 안에 heartbeat 파일을 갱신했는지 확인한다."""
import os
import sys
import time
from pathlib import Path

MAX_AGE_SECONDS = 30


def is_healthy(path: str | Path, max_age: float = MAX_AGE_SECONDS, now: float | None = None) -> bool:
    try:
        mtime = Path(path).stat().st_mtime
    except FileNotFoundError:
        return False
    return ((now if now is not None else time.time()) - mtime) < max_age


def main() -> None:
    sys.exit(0 if is_healthy(os.environ.get("HEARTBEAT_PATH", "/tmp/worker-heartbeat")) else 1)


if __name__ == "__main__":
    main()
