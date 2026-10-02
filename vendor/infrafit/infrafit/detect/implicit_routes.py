"""프레임워크가 자동으로 만드는 라우트(암묵 라우트): knowledge/implicit_routes.yaml 규칙을 저장소에 맞춘다."""

from __future__ import annotations

import re

from infrafit.detect.manifests import Manifests
from infrafit.detect.paths import _config_entries, _spring_configs
from infrafit.detect.testpaths import is_test_path
from infrafit.evidence import line_at
from infrafit.repo import Snapshot, parent_dir

Raw = tuple[str, str, str, int | None, str]  # (메서드, 경로, 근거 파일, 근거 줄, 프레임워크)

_OFF, _UNKNOWN = object(), object()  # 인자가 라우트를 끈다 / 값을 모른다
_OFF_VALUE = re.compile(r"(?:None|null|undefined|false|False)\s*(?:[,)}\]]|$)")
_STR_VALUE = re.compile(r"""(['"`])([^'"`]*)\1\s*(?:[,)}\]]|$)""")


def _close_paren(text: str, start: int) -> int:
    """text[start]의 여는 괄호와 짝이 맞는 닫는 괄호 위치. 문자열 안 괄호는 세지 않는다. 못 찾으면 글 끝."""
    depth, quote, escaped = 0, "", False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def _keyword(args: str, name: str):
    """호출 인자 글에서 이름 붙은 인자 값: 문자열 값, 꺼짐(_OFF), 모름(_UNKNOWN), 없으면 None."""
    m = re.search(rf"(?<![\w.]){re.escape(name)}\s*[=:]\s*", args)
    if m is None:
        return None
    rest = args[m.end():]
    if _OFF_VALUE.match(rest):
        return _OFF
    s = _STR_VALUE.match(rest)
    return s.group(2) if s else _UNKNOWN


def _norm_key(key: str) -> str:
    return re.sub(r"[-_]", "", key.lower())


def _property(snap: Snapshot, module: str, key: str) -> tuple[str, str, int] | None:
    """모듈 Spring 설정에서 key(완화 바인딩)의 (값, 파일, 줄). 기본 파일이 프로필 파일보다 먼저."""
    wanted = _norm_key(key)
    for rel in _spring_configs(snap, module):
        for k, value, line in _config_entries(snap, rel):
            if _norm_key(k) == wanted:
                return value, rel, line
    return None


def _code_value(snap: Snapshot, module: str, code: dict) -> tuple[str, str, int] | None:
    """모듈 아래 테스트가 아닌 코드에서 regex 첫 묶음의 (값, 파일, 줄). 경로 순 첫 번째."""
    rx = re.compile(code["regex"])
    files = sorted({rel for g in code["globs"] for rel in snap.glob(g)
                    if not is_test_path(rel) and (not module or rel.startswith(module + "/"))})
    for rel in files:
        text = snap.read(rel)
        m = rx.search(text)
        if m:
            return m.group(1), rel, line_at(text, m.start())
    return None


def _join(prefix: str, value: str, suffix: str) -> str:
    path = "/".join(p.strip("/") for p in (prefix, value, suffix) if p.strip("/"))
    return "/" + path


def _instances(snap: Snapshot, manifests: Manifests, trigger: dict) -> list[tuple[str, int | None, str, str]]:
    """규칙이 켜진 곳들: (근거 파일, 근거 줄, 모듈 디렉터리, 호출 인자 글)."""
    out = []
    if "dependency" in trigger:
        modules: dict[str, tuple[str, int | None]] = {}
        for dep in trigger["dependency"]:
            for rel, line in sorted(manifests.locations.get(dep.lower(), []), key=lambda x: (x[0], x[1] or 0)):
                if not is_test_path(rel):
                    modules.setdefault(parent_dir(rel), (rel, line))
        return [(rel, line, d, "") for d, (rel, line) in sorted(modules.items())]
    call = trigger["call"]
    if call["dependency"].lower() not in manifests.deps:
        return []
    rx = re.compile(call["regex"])
    for rel in sorted({rel for g in call["globs"] for rel in snap.glob(g)}):
        if is_test_path(rel):
            continue
        text = snap.read(rel)
        for m in rx.finditer(text):
            args = text[m.end():_close_paren(text, m.end() - 1)]
            out.append((rel, line_at(text, m.start()), parent_dir(rel), args))
    return out


def implicit_routes(snap: Snapshot, manifests: Manifests, rules) -> list[Raw]:
    """규칙마다 켜진 곳의 라우트. 같은 (메서드, 경로, 근거 파일)은 하나만, 경로 순서는 규칙·근거 순서."""
    out: list[Raw] = []
    for rule in rules:
        for rel, line, module, args in _instances(snap, manifests, rule["trigger"]):
            deps = manifests.deps_by_dir.get(module, set()) if "dependency" in rule["trigger"] else set(manifests.deps)
            for route in rule["routes"]:
                if route.get("only_with") and not any(d.lower() in deps for d in route["only_with"]):
                    continue
                if any(_keyword(args, k) is _OFF for k in route.get("off_with", [])):
                    continue
                prefix, hole, suffix = route["path"].partition("{}")
                ev_rel, ev_line, value = rel, line, route.get("default", "")
                ov = route.get("override", {})
                if "keyword" in ov:
                    found = _keyword(args, ov["keyword"])
                    if found is _OFF or found is _UNKNOWN:
                        continue
                    value = value if found is None else found
                elif "property" in ov or "code" in ov:
                    hit = (_property(snap, module, ov["property"]) if "property" in ov
                           else _code_value(snap, module, ov["code"]))
                    if hit and "${" in hit[0]:  # 자리표시자 값은 모른다
                        continue
                    if hit:
                        value, ev_rel, ev_line = hit
                path = _join(prefix, value, suffix) if hole else route["path"]
                out.append((route["method"], path, ev_rel, ev_line, rule["framework"]))
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for r in out:
        if r[:3] not in seen:
            seen.add(r[:3])
            unique.append(r)
    return unique
