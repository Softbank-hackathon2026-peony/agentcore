"""코드 파일 → 워크로드 귀속, 핸들러 파일의 로컬 import 따라가기."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import PurePosixPath

from infrafit.detect.signatures import is_aux_path
from infrafit.detect.testpaths import is_test_path
from infrafit.repo import Snapshot, match_glob, parent_dir


def is_skipped(rel: str) -> bool:
    """테스트·보조(scripts/ 등) 경로는 앱 요구의 근거가 아니다."""
    return is_test_path(rel) or is_aux_path(rel)


def _depth(d: str) -> int:
    return len(PurePosixPath(d).parts) if d else 0


def _is_ancestor(d: str, rel: str) -> bool:
    return d == "" or rel.startswith(d + "/")


class Owners:
    """워크로드마다 기준 디렉터리(진입점·근거·엔드포인트 핸들러의 디렉터리, 이름이 워크로드 이름과 같은 디렉터리)를
    모으고, 파일은 그 파일을 품은 가장 깊은 기준 디렉터리의 워크로드(같은 깊이면 모두)에 속한다.
    품은 기준이 없으면 앱 워크로드가 하나일 때만 그 워크로드에 속한다."""

    def __init__(self, snap: Snapshot, inventory: dict, app_ids: list[str]):
        self.app_ids = list(app_ids)
        dirs = {parent_dir(f) for f in snap.files}
        expanded: set[str] = set()
        for d in dirs:
            while d and d not in expanded:
                expanded.add(d)
                d = parent_dir(d)
        by_name: dict[str, list[str]] = {}
        for d in sorted(expanded):
            by_name.setdefault(PurePosixPath(d).name.lower(), []).append(d)
        self.anchors: dict[str, set[str]] = {}
        for w in inventory["workloads"]:
            a = {parent_dir(w["entrypoint"]["path"])}
            a |= {parent_dir(e["path"]) for e in w.get("evidence", [])}
            a |= set(by_name.get(w["name"].lower(), []))
            self.anchors[w["id"]] = a
        for e in inventory["endpoints"]:
            if e["workload"] in self.anchors:
                self.anchors[e["workload"]].add(parent_dir(e["handler"]["path"]))

    def owners(self, rel: str) -> list[str]:
        best, out = -1, []
        for wid in sorted(self.anchors):
            depth = max((_depth(d) for d in self.anchors[wid] if _is_ancestor(d, rel)), default=-1)
            if depth > best:
                best, out = depth, [wid]
            elif depth == best and depth >= 0:
                out.append(wid)
        if best < 0:
            return list(self.app_ids) if len(self.app_ids) == 1 else []
        return [w for w in out if w in self.app_ids]


_PY_FROM = re.compile(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+([\w\s,().*]+)")
_PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)")
_JS_SPEC = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(\s*|\brequire\(\s*|^\s*import\s+)["']([^"']+)["']""")
_JVM_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)")
_JS_EXTS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            "/index.ts", "/index.tsx", "/index.js", "/index.jsx")


class Imports:
    """핸들러 파일에서 저장소 안의 로컬 모듈 파일로 import를 따라간다(Python·JS/TS·Java/Kotlin)."""

    def __init__(self, snap: Snapshot):
        self.snap = snap
        self.files = set(snap.files)
        self.jvm: dict[str, list[str]] = {}
        for f in snap.files:
            if f.endswith((".java", ".kt")):
                p = PurePosixPath(f)
                self.jvm.setdefault(p.stem, []).append(f)
        self._cache: dict[str, tuple[str, ...]] = {}

    def _ancestors(self, rel: str) -> list[str]:
        out, d = [], parent_dir(rel)
        while True:
            out.append(d)
            if not d:
                return out
            d = parent_dir(d)

    def _py_module(self, base: str, parts: list[str]) -> str | None:
        stem = "/".join(p for p in [base, *parts] if p)
        for cand in (stem + ".py", stem + "/__init__.py"):
            if cand in self.files:
                return cand
        return None

    def _python(self, rel: str) -> set[str]:
        out: set[str] = set()
        for line in self.snap.lines(rel):
            m = _PY_FROM.match(line)
            if m:
                dots, mod, names = m.group(1), m.group(2), m.group(3)
                parts = [p for p in mod.split(".") if p]
                names = [n.strip().split()[0] for n in names.replace("(", " ").replace(")", " ").split(",")
                         if n.strip() and n.strip() != "*"]
                if dots:
                    base = parent_dir(rel)
                    for _ in range(len(dots) - 1):
                        base = parent_dir(base)
                    bases = [base]
                else:
                    bases = self._ancestors(rel)
                for base in bases:
                    found = self._py_module(base, parts) if parts else None
                    subs = [self._py_module(base, parts + [n]) for n in names]
                    hits = [f for f in [found, *subs] if f]
                    if hits:
                        out.update(hits)
                        break
                continue
            m = _PY_IMPORT.match(line)
            if m:
                for mod in m.group(1).split(","):
                    parts = mod.strip().split(".")
                    for base in self._ancestors(rel):
                        found = self._py_module(base, parts)
                        if found:
                            out.add(found)
                            break
        return out

    def _js_file(self, stem: str) -> str | None:
        stem = PurePosixPath(stem).as_posix()
        parts: list[str] = []
        for p in stem.split("/"):
            if p == "..":
                if not parts:
                    return None
                parts.pop()
            elif p not in (".", ""):
                parts.append(p)
        stem = "/".join(parts)
        for ext in _JS_EXTS:
            if stem + ext in self.files:
                return stem + ext
        return None

    def _js(self, rel: str) -> set[str]:
        out: set[str] = set()
        for line in self.snap.lines(rel):
            for spec in _JS_SPEC.findall(line):
                if spec.startswith("."):
                    found = self._js_file(f"{parent_dir(rel)}/{spec}" if parent_dir(rel) else spec)
                elif spec[:2] in ("@/", "~/"):
                    found = None
                    for base in self._ancestors(rel):
                        for root in (base, f"{base}/src" if base else "src"):
                            found = self._js_file(f"{root}/{spec[2:]}" if root else spec[2:])
                            if found:
                                break
                        if found:
                            break
                else:
                    found = None
                if found:
                    out.add(found)
        return out

    def _jvm(self, rel: str) -> set[str]:
        out: set[str] = set()
        for line in self.snap.lines(rel):
            m = _JVM_IMPORT.match(line)
            if not m:
                continue
            parts = m.group(1).split(".")
            for cut in (len(parts), len(parts) - 1):  # static import는 마지막 조각이 멤버
                if cut < 2:
                    continue
                suffix = "/".join(parts[:cut])
                hits = [f for f in self.jvm.get(parts[cut - 1], [])
                        if PurePosixPath(f).with_suffix("").as_posix().endswith(suffix)]
                if hits:
                    out.update(hits)
                    break
        return out

    def direct(self, rel: str) -> tuple[str, ...]:
        if rel not in self._cache:
            if rel.endswith(".py"):
                found = self._python(rel)
            elif rel.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
                found = self._js(rel)
            elif rel.endswith((".java", ".kt")):
                found = self._jvm(rel)
            else:
                found = set()
            self._cache[rel] = tuple(sorted(f for f in found if f != rel and not is_skipped(f)))
        return self._cache[rel]

    def closure(self, rel: str, depth: int) -> list[tuple[str, int]]:
        """(파일, 핸들러에서의 거리). 핸들러 파일 자신은 0."""
        seen = {rel: 0}
        frontier = [rel]
        for hop in range(1, depth + 1):
            nxt = []
            for f in frontier:
                for g in self.direct(f):
                    if g not in seen:
                        seen[g] = hop
                        nxt.append(g)
            frontier = sorted(nxt)
        return sorted(seen.items(), key=lambda x: (x[1], x[0]))


@lru_cache(maxsize=256)
def _compiled(rx: str) -> re.Pattern:
    return re.compile(rx)


def code_files(snap: Snapshot, globs: list[str]) -> list[str]:
    return [f for f in snap.files if not is_skipped(f) and any(match_glob(f, g) for g in globs)]


def hit_lines(snap: Snapshot, rel: str, regex: str, unless: str | None = None) -> list[int]:
    """rel에서 regex가 맞는 줄 번호. unless가 파일 어딘가에 맞으면 없음."""
    try:
        text = snap.read(rel)
    except OSError:
        return []
    if unless and _compiled(unless).search(text):
        return []
    rx = _compiled(regex)
    return [i for i, line in enumerate(text.splitlines(), 1) if rx.search(line)]
