"""InfraFit 소스를 vendor/infrafit/ 로 복사한다 (vendoring).

사용: python scripts/sync_infrafit.py <InfraFit 저장소 경로>

복사하는 것: infrafit/ (패키지), knowledge/ (지식 베이스), schemas/ (JSON 스키마).
InfraFit은 knowledge/·schemas/ 를 패키지의 부모 폴더 기준으로 찾으므로 셋을 같은 폴더에 둔다.
vendor/infrafit/SOURCE 에 원본 저장소의 커밋과 날짜를 남긴다.
"""
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "infrafit"
PARTS = ("infrafit", "knowledge", "schemas")


def _git(src: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(src), *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def sync(src: Path) -> Path:
    src = src.resolve()
    missing = [p for p in PARTS if not (src / p).is_dir()]
    if missing:
        raise SystemExit(f"InfraFit 저장소가 아닙니다 ({src}): {', '.join(missing)} 없음")
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    for part in PARTS:
        shutil.copytree(src / part, DEST / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    commit = _git(src, "rev-parse", "HEAD")
    dirty = " (uncommitted changes)" if _git(src, "status", "--porcelain", "--", *PARTS) not in ("", "unknown") else ""
    (DEST / "SOURCE").write_text(
        f"commit: {commit}{dirty}\n"
        f"commit_date: {_git(src, 'log', '-1', '--format=%cI')}\n"
        f"synced_at: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n", encoding="utf-8")
    return DEST


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(f"[sync] {sync(Path(sys.argv[1]))}")
