"""웹 워크로드의 엔드포인트 찾기(설계 §6.2)."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import PurePosixPath

from infrafit import kb
from infrafit.detect.implicit_routes import _close_paren, implicit_routes
from infrafit.detect.jvm import close_bracket, jvm_build_dirs, mask_code, spring_web
from infrafit.detect.manifests import Manifests, parse_manifests
from infrafit.detect.nginx import LocationInfo, ProxyRoute, ProxyServer
from infrafit.detect.proxy_graph import fronted_in, upstream_chains
from infrafit.detect.testpaths import is_test_path
from infrafit.detect.workloads import WorkloadInfo
from infrafit.evidence import evidence, line_at
from infrafit.repo import Snapshot, nearest_dir

HTTP_METHODS = ("get", "post", "put", "delete", "patch")
NEXT_METHODS = "GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS"
_SERVER_VAR = re.compile(
    r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:await\s+)?"
    r"(?:express\s*\(|express\.Router\s*\(|require\(\s*['\"]express['\"]\s*\)(?:\.Router)?\s*\(|Router\s*\("
    r"|fastify\s*\(|Fastify\s*\(|new\s+Koa\s*\(|new\s+Router\s*\(|new\s+Hono\s*\()")
_ROUTE_CALL = re.compile(r"\b(\w+)\.(get|post|put|delete|patch|all)\(\s*['\"`]([^'\"`]+)['\"`]")
# `X.route({ method: ..., url: ... })`(Fastify 등의 라우트 객체)
_ROUTE_OBJECT = re.compile(r"\b(\w+)\.route\(\s*\{")
_OBJ_METHOD = re.compile(r"\bmethod\s*:\s*(\[[^\]]*\]|['\"`][^'\"`]+['\"`])")
_OBJ_URL = re.compile(r"\burl\s*:\s*['\"`]([^'\"`]+)['\"`]")
_STRING = re.compile(r"['\"`]([^'\"`]+)['\"`]")
# 함수 매개변수 목록: `function x(a, b)`, `(a, b) =>`, `(a: T): R =>`, `a =>`
_FUNC_PARAMS = re.compile(r"\bfunction\b\s*\*?\s*\w*\s*\(([^)]*)\)"
                          r"|\(([^()]*)\)\s*(?::\s*[^=;{}()]+)?=>"
                          r"|\b(\w+)\s*=>")
_PARAM_NAME = re.compile(r"^\s*(?:\.\.\.)?(\w+)")
# 가장 가까운 package.json에 이 중 하나가 있으면 매개변수 이름만으로 서버 변수로 본다. 그 이름은 매개변수를
# 선언한 함수 안에서만 서버이고, 경로가 `/`로 시작하는 호출만 라우트로 본다(`api`·`instance`처럼 HTTP
# 클라이언트가 흔히 쓰는 이름은 넣지 않는다)
SERVER_DEPS = ("express", "fastify", "koa", "hono", "@koa/router")
SERVER_PARAMS = frozenset({"app", "router", "server", "fastify"})
# Express 계열 framework 이름: 가장 가까운 package.json 의존성 중 이 순서로 처음 있는 것, 없으면 express
EXPRESS_FAMILY = ("fastify", "koa", "hono", "express")
_DJANGO = re.compile(r"\b(?:re_)?path\(\s*r?['\"]([^'\"]*)['\"]")
_NEXT_EXPORT = re.compile(rf"export\s+(?:async\s+)?function\s+({NEXT_METHODS})\b|export\s+const\s+({NEXT_METHODS})\s*=")

# 경로 매개변수: (?P<x>...), {x}, /:x(세그먼트 시작만), [x](·[...x]·[[...x]]), <x>, <int:x>
_PARAM = re.compile(r"\(\?P<[^>]+>[^)]*\)|\{[^}/]*\}|<[^>/]*>|\[\[?[^\]/]*\]?\]|(?<=/):[A-Za-z_]\w*")
PREFIX_MODIFIERS = ("", "^~")

Raw = tuple[str, str, str, int, str]  # (메서드, 경로, 파일, 줄, 프레임워크)


def _prefixes(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if fname not in ("APIRouter", "Blueprint"):
            continue
        for kw in node.value.keywords:
            if kw.arg in ("prefix", "url_prefix") and isinstance(kw.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = str(kw.value.value)
    return out


_INCLUDE_ATTRS = {"include_router": "prefix", "register_blueprint": "url_prefix"}
_MAX_DEPTH = 5

Ref = tuple[str, str]  # (파일, 라우터 변수명)


def _join(a: str, b: str) -> str:
    """경로 조각을 '/'가 겹치거나 빠지지 않게 잇는다. 한쪽이 비면 다른 쪽 그대로."""
    if not a:
        return b
    if not b:
        return a
    return a.rstrip("/") + "/" + b.lstrip("/")


def _module_file(snap: Snapshot, directory: str, parts: list[str]) -> str | None:
    """directory 기준 점 경로(parts)에 해당하는 모듈 파일(`x.py` 또는 `x/__init__.py`)."""
    if not parts:
        return None
    base = "/".join(([directory] if directory else []) + parts)
    for cand in (f"{base}.py", f"{base}/__init__.py"):
        if snap.exists(cand):
            return cand
    return None


def _abs_module_file(snap: Snapshot, rel: str, parts: list[str]) -> str | None:
    """절대 import: 현재 파일 디렉터리에서 위로 올라가며 처음 찾은 모듈 파일."""
    d = nearest_dir(rel, lambda d: _module_file(snap, d, parts) is not None)
    return None if d is None else _module_file(snap, d, parts)


def _imports(snap: Snapshot, rel: str, tree: ast.AST) -> dict[str, tuple[str, str | None]]:
    """지역 이름 -> (모듈 파일, 원래 이름). 원래 이름이 None이면 모듈 자체를 가리킨다."""
    out: dict[str, tuple[str, str | None]] = {}

    def find(parts: list[str], level: int) -> str | None:
        if level:
            d = PurePosixPath(rel).parent
            for _ in range(level - 1):
                d = d.parent
            base = "" if str(d) == "." else str(d)
            return _module_file(snap, base, parts)
        return _abs_module_file(snap, rel, parts)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module.split(".") if node.module else []
            for alias in node.names:
                local = alias.asname or alias.name
                sub = find(mod + [alias.name], node.level)
                if sub:
                    out[local] = (sub, None)
                    continue
                owner = find(mod, node.level)
                if owner:
                    out[local] = (owner, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.asname and "." in alias.name:
                    continue  # `import a.b`는 이름 a만 묶으므로 다루지 않는다
                f = _abs_module_file(snap, rel, alias.name.split("."))
                if f:
                    out[alias.asname or alias.name] = (f, None)
    return out


def _ref(rel: str, node: ast.expr, imports: dict[str, tuple[str, str | None]]) -> Ref | None:
    """include 대상 표현식을 (파일, 변수명)으로 푼다. 못 풀면 None."""
    if isinstance(node, ast.Name):
        imp = imports.get(node.id)
        if imp is None:
            return (rel, node.id)
        return (imp[0], imp[1]) if imp[1] else None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        imp = imports.get(node.value.id)
        if imp and imp[1] is None:
            return (imp[0], node.attr)
    return None


def _mounts(snap: Snapshot, trees: dict[str, ast.AST]) -> dict[Ref, list[tuple[Ref, str]]]:
    """자식 라우터 -> [(부모 라우터, include 접두어)]. 접두어가 문자열 상수가 아니면 건너뛴다."""
    out: dict[Ref, list[tuple[Ref, str]]] = defaultdict(list)
    for rel, tree in trees.items():
        imports = _imports(snap, rel, tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _INCLUDE_ATTRS and isinstance(node.func.value, ast.Name)
                    and node.args):
                continue
            child = _ref(rel, node.args[0], imports)
            if child is None:
                continue
            prefix = ""
            ok = True
            for kw in node.keywords:
                if kw.arg == _INCLUDE_ATTRS[node.func.attr]:
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        prefix = kw.value.value
                    else:
                        ok = False
            parent = _ref(rel, node.func.value, imports)
            if ok and parent is not None:
                out[child].append((parent, prefix))
    return out


def _include_prefixes(ref: Ref, mounts: dict[Ref, list[tuple[Ref, str]]], own: dict[Ref, str],
                      stack: tuple[Ref, ...] = ()) -> set[str]:
    """라우터가 앱까지 연결되며 받는 include 접두어 전부(중간 라우터 자체 접두어 포함). 순환과 깊이 초과는 끊는다."""
    out: set[str] = set()
    if len(stack) < _MAX_DEPTH:
        for parent, prefix in mounts.get(ref, []):
            if parent == ref or parent in stack:
                continue
            mid = own.get(parent, "")
            out |= {_join(_join(pp, mid), prefix) for pp in _include_prefixes(parent, mounts, own, stack + (ref,))}
    return out or {""}


def _code_files(snap: Snapshot, pattern: str) -> list[str]:
    """pattern에 맞는 파일 중 테스트 코드가 아닌 것."""
    return [rel for rel in snap.glob(pattern) if not is_test_path(rel)]


def _str_arg(call: ast.Call) -> str | None:
    """첫 인자가 문자열 상수면 그 값."""
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _kw_methods(call: ast.Call) -> list[str]:
    """`methods=[...]` 키워드의 문자열 메서드들. 키워드가 없으면 GET."""
    for kw in call.keywords:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            return [str(e.value).upper() for e in kw.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return ["GET"]


def _route_calls(tree: ast.AST):
    """(라우트 호출, 메서드들): 데코레이터 라우트(`@X.get("/r")`, `@X.route("/r", methods=...)`)와
    Flask `X.add_url_rule("/r", methods=...)`. 경로가 문자열 상수인 것만."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)) or _str_arg(dec) is None:
                    continue
                attr = dec.func.attr
                if attr in HTTP_METHODS:
                    yield dec, [attr.upper()]
                elif attr == "route":
                    yield dec, _kw_methods(dec)
                elif attr == "websocket":
                    yield dec, ["WEBSOCKET"]
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "add_url_rule" and _str_arg(node) is not None):
            yield node, _kw_methods(node)


def _py_framework(tree: ast.AST) -> str:
    """파일이 import하는 웹 프레임워크(fastapi, flask). 둘 다 없으면 `python`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return next((fw for fw in ("fastapi", "flask") if fw in names), "python")


def _python(snap: Snapshot) -> list[Raw]:
    trees: dict[str, ast.AST] = {}
    for rel in _code_files(snap, "**/*.py"):
        try:
            trees[rel] = ast.parse(snap.read(rel))
        except (SyntaxError, ValueError, RecursionError):
            continue
    mounts = _mounts(snap, trees)
    own_prefix = {(rel, var): pre for rel, tree in trees.items() for var, pre in _prefixes(tree).items()}
    out: list[Raw] = []
    for rel, tree in trees.items():
        prefixes = _prefixes(tree)
        framework = _py_framework(tree)
        for call, methods in _route_calls(tree):
            owner = call.func.value.id if isinstance(call.func.value, ast.Name) else ""
            own = _join(prefixes.get(owner, ""), _str_arg(call))
            incs = _include_prefixes((rel, owner), mounts, own_prefix) if owner else {""}
            for inc in sorted(incs):
                path = _join(inc, own)
                out += [(m, path, rel, call.lineno, framework) for m in methods]
    return out


def _django(snap: Snapshot) -> list[Raw]:
    out: list[Raw] = []
    for rel in _code_files(snap, "**/urls.py"):
        for i, text in enumerate(snap.lines(rel), 1):
            for m in _DJANGO.finditer(text):
                out.append(("ANY", "/" + m.group(1), rel, i, "django"))
    return out


def _package_deps(snap: Snapshot, manifests: Manifests, rel: str) -> set[str]:
    """파일이 속한 가장 가까운 package.json의 의존성 이름들(없으면 빈 집합)."""
    d = nearest_dir(rel, lambda d: snap.exists(f"{d}/package.json" if d else "package.json"))
    return set() if d is None else manifests.deps_by_dir.get(d, set())


def _block_end(text: str, start: int) -> int:
    """text[start]의 `{`와 짝이 맞는 `}`의 위치. 문자열 안 괄호는 세지 않는다. 못 찾으면 파일 끝."""
    depth = 0
    quote = ""
    escaped = False
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
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


Scope = tuple[str, int, int]  # (서버 이름, 시작 위치, 끝 위치)


def _param_servers(text: str) -> list[Scope]:
    """함수 매개변수 중 서버 변수로 흔히 쓰는 이름(`function x(fastify, opts)`, `async (app) =>` 등)과 그 범위:
    매개변수를 선언한 함수부터 그 함수 본문 끝까지. 본문 끝을 못 찾으면(식 본문 화살표 함수 등) 파일 끝까지."""
    out: list[Scope] = []
    for m in _FUNC_PARAMS.finditer(text):
        params = m.group(1) if m.group(1) is not None else m.group(2) if m.group(2) is not None else m.group(3)
        names = {n.group(1) for p in params.split(",") if (n := _PARAM_NAME.match(p))} & SERVER_PARAMS
        if not names:
            continue
        if m.group(1) is not None:
            brace = text.find("{", m.end())
        else:
            rest = text[m.end():]
            brace = m.end() + len(rest) - len(rest.lstrip()) if rest.lstrip().startswith("{") else -1
        end = _block_end(text, brace) if brace >= 0 else len(text)
        out += [(name, m.start(), end) for name in sorted(names)]
    return out


class _Servers:
    """파일의 서버 변수: 변수 선언으로 찾은 것은 파일 전체, 매개변수로 찾은 것은 그 함수 안에서 `/` 경로만."""

    def __init__(self, declared: set[str], scoped: list[Scope]):
        self.declared = declared
        self.scoped = scoped

    def __bool__(self) -> bool:
        return bool(self.declared or self.scoped)

    def serves(self, name: str, pos: int, path: str) -> bool:
        if name in self.declared:
            return True
        return path.startswith("/") and any(n == name and s <= pos <= e for n, s, e in self.scoped)


def _top_level(text: str, start: int) -> str:
    """text[start]의 `{`로 여는 객체에서 바로 아래 단계 글자만 남긴 문자열. 더 깊은 `{}`·`()` 안은 공백으로
    바꿔 핸들러 본문의 `url:` 같은 글자를 키로 읽지 않는다. 문자열 안 괄호는 세지 않는다."""
    out: list[str] = []
    depth = 0
    quote = ""
    for ch in text[start:]:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"`":
            quote = ch
        elif ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
            if depth == 0:
                break
        out.append(ch if depth == 1 else " ")
    return "".join(out)


def _route_objects(text: str, servers: _Servers) -> list[tuple[list[str], str, int]]:
    """`X.route({ method, url })`(X는 서버 변수) → (메서드들, 경로, 줄)."""
    out = []
    for m in _ROUTE_OBJECT.finditer(text):
        body = _top_level(text, m.end() - 1)
        method, url = _OBJ_METHOD.search(body), _OBJ_URL.search(body)
        if not (method and url and servers.serves(m.group(1), m.start(), url.group(1))):
            continue
        methods = sorted({x.upper() for x in _STRING.findall(method.group(1))})
        out.append((methods, url.group(1), line_at(text, m.start())))
    return out


def _express(snap: Snapshot, manifests: Manifests) -> list[Raw]:
    out: list[Raw] = []
    for pattern in ("**/*.js", "**/*.ts", "**/*.mjs", "**/*.cjs"):
        for rel in _code_files(snap, pattern):
            if PurePosixPath(rel).name.startswith("route.") and "/app/" in f"/{rel}":
                continue
            text = snap.read(rel)
            deps = _package_deps(snap, manifests, rel)
            scoped = _param_servers(text) if any(dep in deps for dep in SERVER_DEPS) else []
            servers = _Servers(set(_SERVER_VAR.findall(text)), scoped)
            if not servers:
                continue
            framework = next((fw for fw in EXPRESS_FAMILY if fw in deps), "express")
            # 여러 줄에 걸친 호출(`app.post(\n  '/x',`)도 읽도록 파일 전체에서 찾고, 줄은 호출 시작 위치로 정한다
            for m in _ROUTE_CALL.finditer(text):
                if not servers.serves(m.group(1), m.start(), m.group(3)):
                    continue
                method = "ANY" if m.group(2) == "all" else m.group(2).upper()
                out.append((method, m.group(3), rel, line_at(text, m.start()), framework))
            for methods, route, i in _route_objects(text, servers):
                out += [(method, route, rel, i, framework) for method in methods]
    return out


def _next_route_path(parts: list[str]) -> str:
    keep = [p for p in parts if not (p.startswith("(") and p.endswith(")")) and not p.startswith("@")]
    return "/" + "/".join(keep)


def _next(snap: Snapshot) -> list[Raw]:
    out: list[Raw] = []
    for ext in ("ts", "js", "tsx", "jsx"):
        for rel in _code_files(snap, f"**/app/**/route.{ext}"):
            parts = PurePosixPath(rel).parts
            idx = len(parts) - 1 - list(reversed(parts)).index("app")
            route = _next_route_path(list(parts[idx + 1:-1]))
            for i, text in enumerate(snap.lines(rel), 1):
                for m in _NEXT_EXPORT.finditer(text):
                    out.append((m.group(1) or m.group(2), route, rel, i, "nextjs"))
    for ext in ("ts", "js"):
        for rel in _code_files(snap, f"**/pages/api/**.{ext}"):
            parts = list(PurePosixPath(rel).with_suffix("").parts)
            idx = len(parts) - 1 - list(reversed(parts)).index("pages")
            segs = parts[idx + 1:]
            if segs and segs[-1] == "index":
                segs = segs[:-1]
            out.append(("ANY", "/" + "/".join(segs), rel, 1, "nextjs"))
    return out


# --- Spring(Java·Kotlin) 컨트롤러 -------------------------------------------------

SPRING_METHODS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE",
                  "PatchMapping": "PATCH"}
REQUEST_METHODS = "GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|TRACE"
_ANNOTATION = re.compile(r"(?<![\w.@])@([A-Za-z_][\w.]*)")
_TYPE_DECL = re.compile(r"(?:(?:public|private|protected|internal|open|final|abstract|sealed|data|static|inner"
                        r"|enum|annotation|value)\s+)*(?:class|interface|object|enum|record)\b")
# 타입 선언 키워드(`Foo.class`, `@interface`, `companion object`는 아니다)
_TYPE_KEYWORD = re.compile(r"(?<![\w.@$])(?<!companion )(?:class|interface|object|enum|record)\s+[A-Za-z_]")
_JAVA_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
def _expr_end(struct: str, start: int) -> int:
    """start에서 시작하는 값 식의 끝: 괄호 식(`{..}`·`[..]`·`arrayOf(..)`)이면 짝 괄호 뒤, 아니면 다음 `,` 앞."""
    m = re.match(r"\s*(?:arrayOf\s*)?([(\[{])", struct[start:])
    if m:
        return close_bracket(struct, start + m.start(1)) + 1
    comma = struct.find(",", start)
    return len(struct) if comma < 0 else comma


def _mapping_paths(clean: str, struct: str) -> list[str] | None:
    """매핑 어노테이션 인자(괄호 안 글)의 경로들. 인자가 없거나 경로 인자가 없으면 [""],
    경로가 문자열 상수가 아니면(상수 참조 등) None."""
    if not struct.strip():
        return [""]
    named = re.search(r"\b(?:value|path)\s*=\s*", struct)
    if named:
        start = named.end()
    elif re.match(r"\s*(?:\"|\{|\[|arrayOf\b)", struct):
        start = 0
    elif re.match(r"\s*\w+\s*=", struct):  # 이름 붙은 다른 인자만 있다(method =, produces = 등)
        return [""]
    else:
        return None
    end = _expr_end(struct, start)
    # 문자열 밖 `+`(이어 붙이기)나 `$`(문자열 템플릿·속성 자리표시자)로 만든 경로는 값을 모른다
    if "+" in struct[start:end] or "$" in clean[start:end]:
        return None
    paths = _JAVA_STRING.findall(clean[start:end])
    return paths or None


def _mapping_methods(struct: str) -> list[str]:
    """`@RequestMapping`의 `method =` 값의 HTTP 메서드들, 없으면 ANY."""
    m = re.search(r"\bmethod\s*=\s*", struct)
    found = re.findall(rf"\b({REQUEST_METHODS})\b", struct[m.end():_expr_end(struct, m.end())]) if m else []
    return sorted(set(found)) or ["ANY"]


def _annotation_groups(clean: str, struct: str):
    """(어노테이션들 [(이름, 인자 clean, 인자 struct, 위치)], 뒤따르는 타입 선언 범위 또는 None).
    공백으로만 이어진 어노테이션을 한 묶음으로 보고, 묶음 바로 뒤가 타입 선언이면 클래스 묶음이다."""
    i, group = 0, []
    while (m := _ANNOTATION.search(struct, i)) is not None:
        name = m.group(1).rsplit(".", 1)[-1]
        end = m.end()
        args = ("", "")
        paren = re.match(r"\s*\(", struct[end:])
        if paren and name != "interface":
            close = close_bracket(struct, end + paren.end() - 1)
            args = (clean[end + paren.end():close], struct[end + paren.end():close])
            end = close + 1
        group.append((name, args[0], args[1], m.start()))
        rest = struct[end:].lstrip()
        if not rest.startswith("@"):
            decl = _TYPE_DECL.match(rest)
            start = len(struct) - len(rest)
            yield group, (start, start + decl.end()) if decl else None
            group = []
        i = max(end, m.end())


def _spring_framework(rel: str, build_dirs: set[str], manifests: Manifests) -> str:
    """파일을 품은 빌드 파일 디렉터리(가까운 것부터)의 Spring 웹 스타터 프레임워크, 없으면 spring."""
    d = nearest_dir(rel, lambda d: d in build_dirs and spring_web(manifests.deps_by_dir.get(d, set())) is not None)
    return "spring" if d is None else spring_web(manifests.deps_by_dir.get(d, set()))[1]


def _spring(snap: Snapshot, manifests: Manifests) -> list[Raw]:
    """`@RestController`·`@Controller` 클래스의 매핑 메서드. 클래스 `@RequestMapping` 경로를 접두어로 쓴다.
    중첩 클래스와 주석 안 어노테이션은 다루지 않는다."""
    build_dirs = set(jvm_build_dirs(snap))
    out: list[Raw] = []
    for rel in _code_files(snap, "**/*.java") + _code_files(snap, "**/*.kt"):
        text = snap.read(rel)
        if "Controller" not in text:
            continue
        clean, struct = mask_code(text)
        framework = _spring_framework(rel, build_dirs, manifests)
        controller, prefixes = False, None
        groups = list(_annotation_groups(clean, struct))
        spans = [decl for _, decl in groups if decl]
        # 어노테이션 없는 타입 선언도 상태를 바꾼다(그 클래스의 매핑은 컨트롤러가 아니다)
        # `object`는 Kotlin에서만 선언이다(Java에서는 흔한 변수 이름)
        bare = [([], (k.start(), k.start())) for k in _TYPE_KEYWORD.finditer(struct)
                if not any(a <= k.start() < b for a, b in spans)
                and (rel.endswith(".kt") or not k.group().startswith("object"))]
        events = sorted(groups + bare, key=lambda e: e[0][0][3] if e[0] else e[1][0])
        for group, decl in events:
            names = {g[0] for g in group}
            if decl:
                controller = bool(names & {"RestController", "Controller"})
                mapping = next((g for g in group if g[0] == "RequestMapping"), None)
                prefixes = _mapping_paths(mapping[1], mapping[2]) if mapping else [""]
                continue
            if not controller or prefixes is None:
                continue
            for name, args, args_struct, pos in group:
                if name in SPRING_METHODS:
                    methods = [SPRING_METHODS[name]]
                elif name == "RequestMapping":
                    methods = _mapping_methods(args_struct)
                else:
                    continue
                line = line_at(text, pos)
                for prefix in prefixes:
                    for path in _mapping_paths(args, args_struct) or []:
                        route = _join(prefix, path)
                        route = route if route.startswith("/") else "/" + route
                        out += [(method, route, rel, line, framework) for method in methods]
    return out


# --- 웹소켓 엔드포인트 -----------------------------------------------------------

_STOMP_ENDPOINT = re.compile(r"\.addEndpoint\s*\(")
_SERVER_ENDPOINT = re.compile(r"(?<![\w.])@(?:[\w.]+\.)?ServerEndpoint\s*\(")


def _spring_websocket(snap: Snapshot, manifests: Manifests) -> list[Raw]:
    """Spring STOMP `registry.addEndpoint("/x", ...)`와 `@ServerEndpoint("/x")`. 경로가 문자열 상수인 것만."""
    build_dirs = set(jvm_build_dirs(snap))
    out: list[Raw] = []
    for rel in _code_files(snap, "**/*.java") + _code_files(snap, "**/*.kt"):
        text = snap.read(rel)
        if "addEndpoint" not in text and "ServerEndpoint" not in text:
            continue
        clean, struct = mask_code(text)
        framework = _spring_framework(rel, build_dirs, manifests)
        for rx, annotation in ((_STOMP_ENDPOINT, False), (_SERVER_ENDPOINT, True)):
            for m in rx.finditer(struct):
                close = close_bracket(struct, m.end() - 1)
                args, args_struct = clean[m.end():close], struct[m.end():close]
                if annotation:
                    paths = _mapping_paths(args, args_struct) or []
                elif "+" in args_struct or "$" in args:
                    paths = []
                else:
                    paths = _JAVA_STRING.findall(args)
                out += [("WEBSOCKET", p if p.startswith("/") else "/" + p, rel, line_at(text, m.start()), framework)
                        for p in paths if p]
    return out


_SOCKETIO_IMPORT = re.compile(r"""(?:\bfrom\s+|\brequire\s*\(\s*)['"]socket\.io['"]""")
_SOCKETIO_NAMES = re.compile(r"""\bimport\s+(?:\*\s+as\s+)?(\w+)\s*(?:,\s*\{[^}]*\})?\s*from\s+['"]socket\.io['"]"""
                             r"""|\b(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*['"]socket\.io['"]\s*\)\s*[;\n]""")
_SOCKETIO_ALIAS = re.compile(r"\bServer\s+as\s+(\w+)")
_SOCKETIO_PATH = re.compile(r"""\bpath\s*:\s*['"`]([^'"`]+)['"`]""")


def _socketio(snap: Snapshot) -> list[Raw]:
    """socket.io 서버: `new Server(`(별칭·`ns.Server` 포함), 기본 가져오기 이름 호출 `io(`,
    `require("socket.io")(`. socket.io를 가져오는 파일만 본다(socket.io-client는 아니다). 옵션 `path`, 없으면 `/socket.io`."""
    out: list[Raw] = []
    for pattern in ("**/*.js", "**/*.ts", "**/*.mjs", "**/*.cjs"):
        for rel in _code_files(snap, pattern):
            text = snap.read(rel)
            if not _SOCKETIO_IMPORT.search(text):
                continue
            names = {n for m in _SOCKETIO_NAMES.finditer(text) for n in m.groups() if n}
            classes = {"Server"} | set(_SOCKETIO_ALIAS.findall(text))
            # `ns.Server`는 ns가 socket.io를 가져온 이름일 때만(`new http.Server(`는 아니다)
            ns = rf"(?:(?:{'|'.join(sorted(names))})\.)?" if names else ""
            alts = [rf"\bnew\s+(?<![\w.]){ns}(?:{'|'.join(sorted(classes))})\s*\(",
                    r"""\brequire\s*\(\s*['"]socket\.io['"]\s*\)\s*\("""]
            if names:
                alts.append(rf"(?<![\w.])(?:new\s+)?(?:{'|'.join(sorted(names))})\s*\(")
            for m in re.finditer("|".join(alts), text):
                args = text[m.end():_close_paren(text, m.end() - 1)]
                path = _SOCKETIO_PATH.search(args)
                out.append(("WEBSOCKET", path.group(1) if path else "/socket.io", rel, line_at(text, m.start()),
                            "socket.io"))
    return out


def _assign(rel: str, webs: list[WorkloadInfo]) -> tuple[WorkloadInfo, bool]:
    """(워크로드, 근거로 정했는가). web 워크로드가 여럿일 때 쓴다. 아무 근거도 없어 첫 워크로드로 보낸 것은 추측이다."""
    segments = set(PurePosixPath(rel).parts)

    def score(w: WorkloadInfo) -> int:
        s = len(set(w.name.split("-")) & segments)
        if any(root and rel.startswith(root + "/") for root in {w.app_dir, w.code_root}):
            s += 10
        return s

    best = max(webs, key=lambda w: (score(w), -len(w.name), [-ord(c) for c in w.id]))
    if score(best) > 0:
        return best, True
    return sorted(webs, key=lambda w: w.id)[0], False


def _roots(w: WorkloadInfo) -> set[str]:
    """워크로드 코드의 뿌리 디렉터리들. code_root는 `""`(저장소 루트)도 뿌리이고 None만 모름이다.
    app_dir는 코드에서 찾은 워크로드만 채우고 나머지는 기본값 `""`라서, 비어 있지 않을 때만 뿌리로 본다
    (코드·Dockerfile 워크로드는 code_root가 app_dir와 같아 루트 앱도 code_root로 잡힌다)."""
    return {r for r in (w.app_dir or None, w.code_root) if r is not None}


def _root_depth(rel: str, w: WorkloadInfo) -> int | None:
    """rel을 품은 워크로드 뿌리 중 가장 깊은 것의 깊이(품지 않으면 None)."""
    roots = [r for r in _roots(w) if r == "" or rel.startswith(r + "/")]
    return max(len(PurePosixPath(r).parts) for r in roots) if roots else None


def _owned_by_other(rel: str, webs: list[WorkloadInfo], others: list[WorkloadInfo]) -> bool:
    """핸들러 파일이 web이 아닌 워크로드(워커 등)의 코드 뿌리 안에 어느 web 뿌리보다 깊게 있으면 그 워크로드의
    코드다. 그 HTTP 라우트는 web 엔드포인트가 아니다. 코드 뿌리를 모르는 web 워크로드가 있으면 그 코드일 수 있으므로
    버리지 않는다(이미지만 있는 compose·k8s 앱)."""
    if any(not _roots(w) for w in webs):
        return False
    other = max((d for w in others if (d := _root_depth(rel, w)) is not None), default=None)
    if other is None:
        return False
    web = max((d for w in webs if (d := _root_depth(rel, w)) is not None), default=-1)
    return other > web


def _owners(rel: str, webs: list[WorkloadInfo]) -> list[WorkloadInfo]:
    """핸들러 파일을 app_dir 또는 code_root 아래에 둔 web 워크로드 중 그 루트가 가장 깊은 것들(같으면 모두, id 순)."""
    depth: dict[str, int] = {}
    for w in webs:
        roots = [r for r in _roots(w) if r == "" or rel.startswith(r + "/")]
        if roots:
            depth[w.id] = max(len(PurePosixPath(r).parts) for r in roots)
    if not depth:
        return []
    deepest = max(depth.values())
    return sorted((w for w in webs if depth.get(w.id) == deepest), key=lambda w: w.id)


# --- nginx location 선택과 노출 판정 -------------------------------------------

def _matches(loc: LocationInfo, path: str) -> bool:
    flags = re.IGNORECASE if loc.modifier == "~*" else 0
    try:
        return re.search(loc.pattern, path, flags) is not None
    except re.error:  # PCRE 전용 문법 등은 건너뛴다
        return False


REGEX_MODIFIERS = ("~", "~*")


def _nested(loc: LocationInfo, request_path: str) -> LocationInfo:
    return (select_location(loc.children, request_path) if loc.children else None) or loc


def select_location(locations: list[LocationInfo], request_path: str) -> LocationInfo | None:
    """nginx 규칙: `=` 정확 일치 → 가장 긴 접두어(`^~`면 확정) → 그 접두어 안의 중첩 정규식 → 바깥 정규식(설정 순서)
    → 기억한 접두어(또는 그 안의 중첩 선택). 명명 location(`@x`)만 후보에서 뺀다. internal location도 고를 수 있고,
    고른 것이 internal이면 외부 요청은 아무 데도 닿지 않는다(nginx 404)."""
    cands = [loc for loc in locations if loc.modifier != "@"]
    exact = next((loc for loc in cands if loc.modifier == "=" and loc.pattern == request_path), None)
    if exact is not None:
        return _nested(exact, request_path)
    prefixes = [loc for loc in cands if loc.modifier in PREFIX_MODIFIERS and request_path.startswith(loc.pattern)]
    best = max(prefixes, key=lambda loc: (len(loc.pattern), -loc.order), default=None)
    if best is not None and best.modifier == "^~":
        return _nested(best, request_path)
    inner = select_location(best.children, request_path) if best is not None and best.children else None
    if inner is not None and inner.modifier in REGEX_MODIFIERS:
        return inner
    regexes = sorted((loc for loc in cands if loc.modifier in REGEX_MODIFIERS), key=lambda loc: loc.order)
    regex = next((loc for loc in regexes if _matches(loc, request_path)), None)
    if regex is not None:
        return _nested(regex, request_path)
    return inner or best


def _request_path(route: str, framework: str = "") -> str:
    if framework == "django":  # 정규식 경로의 앵커는 요청 경로에 없다
        route = route.replace("^", "").removesuffix("$")
    path = _PARAM.sub("x1", route)
    return path if path.startswith("/") else "/" + path


def _forwarded(loc: LocationInfo, uri: str | None, path: str) -> str:
    """location이 uri를 가진 proxy_pass로 넘길 때 upstream이 받는 경로."""
    if uri is None:
        return path
    rest = path[len(loc.pattern):] if loc.modifier in PREFIX_MODIFIERS and path.startswith(loc.pattern) else ""
    return uri + rest


def _all_locations(locations: list[LocationInfo]) -> list[LocationInfo]:
    out = []
    for loc in locations:
        out.append(loc)
        out.extend(_all_locations(loc.children))
    return out


def _reached(server: ProxyServer, path: str) -> set[tuple[str, str]]:
    """외부 요청 path가 이 server에서 고른 location으로 닿는 (워크로드 id, 전달 경로)들."""
    loc = select_location(server.locations, path)
    if loc is None or loc.internal:  # internal location을 고른 외부 요청은 404
        return set()
    return {(t, _forwarded(loc, uri, path)) for t, uri in loc.proxies if t}


def _external(locations: list[LocationInfo]) -> list[LocationInfo]:
    """외부 요청이 고를 수 있는 location(internal·명명 제외, 그 안의 중첩 포함)."""
    out = []
    for loc in locations:
        if loc.modifier != "@" and not loc.internal:
            out.append(loc)
            out.extend(_external(loc.children))
    return out


def _requests_for(server: ProxyServer, workload: str, path: str) -> list[str]:
    """upstream 경로 path가 workload에 닿을 수 있는 외부 요청 후보: path 자체, 그리고 workload로 uri U를 붙여
    넘기는 외부 접두사 location L마다 path가 U로 시작하면 L.pattern + path[len(U):](접두사를 떼는 프록시)."""
    out = [path]
    for loc in _external(server.locations):
        if loc.modifier not in PREFIX_MODIFIERS:
            continue
        for t, uri in loc.proxies:
            if t == workload and uri is not None and path.startswith(uri):
                out.append(loc.pattern + path[len(uri):])
    return sorted(set(out))


def _subrequest_reached(server: ProxyServer) -> set[tuple[str, str]]:
    """외부에서 고를 수 있는 location의 하위 요청(auth_request)은 호출 엔드포인트와 상관없이 닿는다."""
    internal = [x for x in _all_locations(server.locations) if x.internal]
    out: set[tuple[str, str]] = set()
    for loc in _external(server.locations):
        for name in loc.subrequests:
            for sub in (x for x in internal if x.pattern == name):
                out |= {(t, _forwarded(sub, uri, name)) for t, uri in sub.proxies if t}
    return out


class _Reach:
    """한 환경에서 프록시에 들어온 요청 경로가 닿는 (워크로드 id, 전달 경로)들을 기억해 둔다."""

    def __init__(self, servers: list[ProxyServer]):
        self.by_proxy: dict[str, list[ProxyServer]] = defaultdict(list)
        for server in servers:
            self.by_proxy[server.proxy].append(server)
        self._memo: dict[tuple, frozenset] = {}

    def server(self, proxy: str, i: int, path: str) -> frozenset:
        key = (proxy, i, path)
        if key not in self._memo:
            self._memo[key] = frozenset(_reached(self.by_proxy[proxy][i], path))
        return self._memo[key]

    def proxy(self, proxy: str, path: str) -> frozenset:
        key = (proxy, None, path)
        if key not in self._memo:
            self._memo[key] = frozenset().union(*(self.server(proxy, i, path)
                                                  for i in range(len(self.by_proxy.get(proxy, [])))))
        return self._memo[key]


def _chain_reached(chain: tuple[str, ...], top: int, reach: _Reach, workload: str, path: str) -> set[tuple[str, str]]:
    """체인 맨 위 프록시의 top번째 server로 들어온 외부 요청 후보가 체인 끝 프록시에서 닿는 (워크로드 id, 전달 경로)들.
    후보는 workload의 upstream 경로 path에서 시작해 아래 단계부터 위로 역매핑해 만들고(_requests_for),
    맨 위에서부터 단계마다 다시 location을 골라 다음 프록시로 넘어가는 것만 따라간다(internal을 고르면 404)."""
    targets = chain[1:] + (workload,)
    cands = {path}
    for i in range(len(chain) - 1, 0, -1):
        cands |= {c for server in reach.by_proxy.get(chain[i], []) for r in cands
                  for c in _requests_for(server, targets[i], r)}
    top_server = reach.by_proxy[chain[0]][top]
    cands = {c for r in cands for c in _requests_for(top_server, targets[0], r)}
    reached: set[tuple[str, str]] = set()
    for r in cands:
        reached |= reach.server(chain[0], top, r)
    for i in range(1, len(chain)):
        forwarded = {fwd for t, fwd in reached if t == chain[i]}
        reached = {hit for r in forwarded for hit in reach.proxy(chain[i], r)}
    return reached


def _exposure_in(env: str | None, out: list[dict], routes: list[ProxyRoute], servers: list[ProxyServer],
                 fronted: set[str]) -> dict[str, str]:
    """환경 env의 server·route만 써서, 그 환경 route가 target으로 하는 워크로드의 엔드포인트 id별 값을 계산한다.
    프록시 뒤의 프록시는 체인 맨 위 프록시로 들어온 외부 요청이 단계마다 전달되어 닿는지로 본다.
    fronted는 이 환경에서 앞 구간(엣지·로드밸런서)이 있는 프록시 id들이다."""
    routes = [r for r in routes if r.environment == env]
    servers = [s for s in servers if s.environment == env]
    routed = {r.target for r in routes if r.target}
    reach = _Reach(servers)
    # 하위 요청(auth_request)은 요청이 들어오는 프록시마다 무조건 닿는다. 체인 맨 위가 아닌 프록시도
    # 위 단계 route로 요청을 받으므로 환경의 모든 server를 본다
    reached: set[tuple[str, str]] = set()
    for server in servers:
        reached |= _subrequest_reached(server)
    chains = {w: sorted(upstream_chains(routes, fronted, w)) for w in sorted(routed)}
    for ep in out:
        if ep["workload"] not in routed:
            continue
        path = _request_path(ep["route"], ep["framework"])
        for chain in chains[ep["workload"]]:
            for top in range(len(reach.by_proxy.get(chain[0], []))):
                reached |= _chain_reached(chain, top, reach, ep["workload"], path)
    return {ep["id"]: "routed" if (ep["workload"], _request_path(ep["route"], ep["framework"])) in reached
            else "not-routed" for ep in out if ep["workload"] in routed}


def _mark_exposure(out: list[dict], routes: list[ProxyRoute], servers: list[ProxyServer],
                   fronted: set[tuple[str, str | None]]) -> None:
    """엔드포인트마다 exposure를 [{environment, value}]로 쓴다. route가 없는 환경은 넣지 않는다."""
    envs = sorted({r.environment for r in routes if r.target}, key=lambda e: (e is not None, e or ""))
    for env in envs:
        values = _exposure_in(env, out, routes, servers, fronted_in(fronted, env))
        for ep in out:
            if ep["id"] in values:
                ep.setdefault("exposure", []).append({"environment": env, "value": values[ep["id"]]})


def extract_endpoints(snap: Snapshot, workloads: list[WorkloadInfo], routes: list[ProxyRoute] | None = None,
                      servers: list[ProxyServer] | None = None,
                      fronted: set[tuple[str, str | None]] | None = None,
                      manifests: Manifests | None = None) -> list[dict]:
    """fronted: 앞 구간이 있는 (프록시 id, 환경 이름). 프록시 체인이 거기서 끝난다(paths.fronted_proxies).
    manifests: Express 계열 서버 변수·framework 판정에 쓰는 의존성(없으면 여기서 읽는다)."""
    # 엔드포인트는 web 워크로드에만 배정한다(리버스 프록시·정적 프런트엔드 제외)
    webs = [w for w in workloads if w.kind == "web"]
    if not webs:
        return []
    if manifests is None:
        manifests = parse_manifests(snap)
    found = sorted(set(_python(snap) + _django(snap) + _express(snap, manifests) + _next(snap)
                       + _spring(snap, manifests) + _spring_websocket(snap, manifests) + _socketio(snap)),
                   key=lambda r: (r[2], r[3], r[0], r[1], r[4]))
    # 같은 (메서드, 경로, 파일, 줄)은 하나만 둔다
    seen: set[tuple[str, str, str, int]] = set()
    raw = []
    for r in found:
        if r[:4] not in seen:
            seen.add(r[:4])
            raw.append(r)
    # 암묵 라우트는 코드 라우트 뒤에 둔다(코드 라우트의 id가 그대로다). 모두 후보다
    build_dirs = set(jvm_build_dirs(snap))
    implicit = [(m, path, rel, line, _spring_framework(rel, build_dirs, manifests) if fw == "spring" else fw)
                for m, path, rel, line, fw in implicit_routes(snap, manifests, kb.implicit_routes())]
    counters: dict[str, int] = defaultdict(int)
    out: list[dict] = []
    others = [w for w in workloads if w.kind != "web"]
    for i, (method, route, rel, line, framework) in enumerate(raw + implicit):
        guessed = i >= len(raw)
        if _owned_by_other(rel, webs, others):
            continue
        # 여러 워크로드가 같은 코드를 쓰면 근거가 있는 워크로드마다 하나씩, 없으면 점수·대체 규칙
        owners = _owners(rel, webs)
        # web 워크로드가 하나면 code_root 추측과 상관없이 그것이 소유자다(확정). 여럿일 때 하나뿐인 Dockerfile로
        # 추측한 code_root로 정한 소유자는 후보다
        if len(webs) == 1:
            targets = [(webs[0], True)]
        elif owners:
            targets = [(w, not w.root_guessed) for w in owners]
        else:
            targets = [_assign(rel, webs)]
        for w, sure in targets:
            if guessed and any(e["workload"] == w.id and e["method"] == method and e["route"] == route for e in out):
                continue  # 코드가 같은 라우트를 직접 만든다
            sure = sure and not guessed
            counters[w.id] += 1
            out.append({"id": f"ep-{w.id[2:]}-{counters[w.id]:03d}", "workload": w.id, "method": method,
                        "route": route, "handler": evidence(snap, rel, line), "framework": framework,
                        "status": "confirmed" if sure else "candidate"})
    if routes:
        _mark_exposure(out, routes, servers or [], fronted or set())
    return out
