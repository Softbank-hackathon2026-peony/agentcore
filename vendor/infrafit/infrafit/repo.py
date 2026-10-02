"""저장소 스냅샷: 분석 대상 파일 목록과 읽기."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

EXCLUDED_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
    ".nuxt", "vendor", ".terraform", "coverage", ".pytest_cache", ".mypy_cache", "out",
})
BINARY_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz", ".tgz",
    ".woff", ".woff2", ".ttf", ".eot", ".db", ".sqlite", ".sqlite3", ".mp4", ".mp3",
})
MAX_TEXT_BYTES = 1_000_000
LANG_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".go": "go",
    ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".rs": "rust",
    ".cs": "csharp",
}


def parent_dir(rel: str) -> str:
    """저장소 경로의 디렉터리(루트는 "")."""
    parent = PurePosixPath(rel).parent.as_posix()
    return "" if parent == "." else parent


def nearest_dir(rel: str, accept: Callable[[str], bool]) -> str | None:
    """rel의 디렉터리부터 저장소 루트("")까지 올라가며 accept가 참인 첫 디렉터리. 없으면 None."""
    d = parent_dir(rel)
    while not accept(d):
        if not d:
            return None
        d = parent_dir(d)
    return d


def match_glob(path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])


@dataclass
class Snapshot:
    root: Path
    repo: str
    commit: str
    files: list[str]
    files_total: int
    excluded: list[str]
    _cache: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._fileset = set(self.files)

    def read(self, rel: str) -> str:
        if rel not in self._cache:
            self._cache[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        return self._cache[rel]

    def lines(self, rel: str) -> list[str]:
        return self.read(rel).splitlines()

    def glob(self, pattern: str) -> list[str]:
        return [f for f in self.files if match_glob(f, pattern)]

    def exists(self, rel: str) -> bool:
        return rel in self._fileset


def _walk(root: Path, exclude: Path | None = None) -> tuple[list[str], list[str], int]:
    files: list[str] = []
    excluded: list[str] = []
    total = 0
    # Compute exclude relative path if it's inside root
    exclude_rel = None
    if exclude is not None:
        try:
            exclude_rel = exclude.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        keep = []
        for d in sorted(dirnames):
            dir_path = Path(dirpath) / d
            # Skip symlinks to directories
            if dir_path.is_symlink():
                continue
            # Skip excluded directories
            if d in EXCLUDED_DIRS:
                excluded.append(f"{d}/" if rel_dir == "." else f"{rel_dir}/{d}/")
            # Skip the exclude directory (exact match only)
            elif exclude_rel is not None:
                rel_d = f"{rel_dir}/{d}" if rel_dir != "." else d
                if rel_d == exclude_rel:
                    continue
                else:
                    keep.append(d)
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = Path(dirpath) / name
            # Skip symlinks to files
            if path.is_symlink():
                continue
            total += 1
            if path.suffix.lower() in BINARY_EXTS or path.stat().st_size > MAX_TEXT_BYTES:
                continue
            files.append(path.relative_to(root).as_posix())
    return sorted(files), sorted(excluded), total


def content_digest(root: Path, files: list[str]) -> str:
    """스캔한 모든 파일의 경로와 내용 해시(커밋되지 않은 변경 포함)."""
    digest = hashlib.sha256()
    for rel in files:
        digest.update(rel.encode("utf-8"))
        digest.update(hashlib.sha256((root / rel).read_bytes()).digest())
    return digest.hexdigest()


def _commit(root: Path, files: list[str]) -> str:
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        if Path(top).resolve() == root.resolve():
            return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return "tree-" + content_digest(root, files)[:16]


def open_snapshot(source: str, workdir: Path, exclude: Path | None = None) -> Snapshot:
    if source.startswith(("https://", "http://", "git@")):
        dest = workdir / "source"
        if not dest.exists():
            workdir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--depth", "1", source, str(dest)],
                           capture_output=True, check=True)
        root, repo = dest, source
    else:
        root = Path(source).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"저장소 경로가 없음: {source}")
        repo = str(root)
    files, excluded, total = _walk(root, exclude)
    return Snapshot(root=root, repo=repo, commit=_commit(root, files), files=files,
                    files_total=total, excluded=excluded)


def language_ratios(files: list[str]) -> dict[str, float]:
    counts = Counter(LANG_BY_EXT[Path(f).suffix.lower()] for f in files
                     if Path(f).suffix.lower() in LANG_BY_EXT)
    total = sum(counts.values())
    return {k: round(v / total, 3) for k, v in sorted(counts.items())} if total else {}
