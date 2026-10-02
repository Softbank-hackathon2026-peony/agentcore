"""근거(Evidence) 만들기와 줄 검색."""

from __future__ import annotations

import re
from bisect import bisect_right
from functools import lru_cache
from itertools import accumulate

from infrafit.repo import Snapshot


def evidence(snap: Snapshot, rel: str, line: int | None = None, kind: str | None = None) -> dict:
    snippet = rel
    if line is not None:
        lines = snap.lines(rel)
        if 0 < line <= len(lines):
            snippet = lines[line - 1].strip()[:200] or rel
    ev = {"path": rel, "line": line, "snippet": snippet}
    if kind:
        ev["kind"] = kind
    return ev


def line_of(snap: Snapshot, rel: str, needle: str) -> int | None:
    for i, text in enumerate(snap.lines(rel), 1):
        if needle in text:
            return i
    return None


def grep_lines(snap: Snapshot, rel: str, pattern: str, flags: int = 0) -> list[int]:
    rx = re.compile(pattern, flags)
    return [i for i, text in enumerate(snap.lines(rel), 1) if rx.search(text)]


@lru_cache(maxsize=16)
def _line_ends(text: str) -> list[int]:
    """줄마다 끝 위치(줄바꿈 다음 글자). 같은 글을 여러 번 물어도 한 번만 계산한다."""
    return list(accumulate(len(line) for line in text.splitlines(keepends=True)))


def line_at(text: str, pos: int) -> int:
    """text의 pos 글자가 있는 줄 번호(1부터, Snapshot.lines와 같은 줄 나눔)."""
    return bisect_right(_line_ends(text), pos) + 1
