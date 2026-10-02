"""nginx 리버스 프록시 설정 읽기와 upstream → 워크로드 해석."""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import crossplane

from infrafit.detect.artifacts import ParsedArtifact, as_dict, final_chain, dockerfile_stages, is_build_path
from infrafit.detect.environments import (Environment, compose_env, env_scopes, env_service, is_workload_doc,
                                          workload_container)
from infrafit.detect.images import classify_image
from infrafit.detect.workloads import WorkloadInfo, workload_dockerfile
from infrafit.evidence import evidence
from infrafit.repo import Snapshot, parent_dir

CONF_SUFFIXES = (".conf", ".conf.template", ".conf.tmpl")
PROXY_DIRECTIVES = {"server", "upstream", "location", "proxy_pass"}
TIME_KEYS = ("proxy_read_timeout", "proxy_send_timeout", "proxy_connect_timeout", "keepalive_timeout")
SETTING_KEYS = TIME_KEYS + ("client_max_body_size", "proxy_http_version")
NGINX_ROOT = "/etc/nginx/"
NGINX_MAIN = "/etc/nginx/nginx.conf"
NGINX_COMPONENT = "nw:proxy/nginx/default"
TEMPLATE_DIR = "/etc/nginx/templates/"
CONFD_DIR = "/etc/nginx/conf.d"
MAX_INCLUDE_DEPTH = 10
# 밀리초 단위(정수 계산으로 부동소수 오차를 피한다)
TIME_UNITS_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000,
                 "M": 2_592_000_000, "y": 31_536_000_000}
MAX_EXPANDED = 10_000  # 진입 설정 하나에서 펼칠 지시어·include 방문 수 상한
_TIME = re.compile(r"(\d+)(ms|s|m|h|d|w|M|y)?")
_ENVSUBST = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PROXY_PASS = re.compile(r"^(?:https?|grpcs?)://([^/]*)(/.*)?$")
_MODIFIER = re.compile(r"^(=|~\*|~|\^~)(.+)$")


@dataclass
class LocationInfo:
    modifier: str  # "", "=", "~", "~*", "^~", "@"
    pattern: str
    internal: bool
    order: int  # server 안에서의 설정 순서(정규식 검사 순서)
    proxies: list[tuple[str | None, str | None]] = field(default_factory=list)  # (대상 워크로드 id, uri)
    subrequests: list[str] = field(default_factory=list)
    children: list[LocationInfo] = field(default_factory=list)


@dataclass
class ProxyServer:
    proxy: str
    environment: str | None
    settings: list[dict]  # server 단위 실효 설정(server → http)
    locations: list[LocationInfo]  # 맨 위 location들(중첩은 children)
    evidence: dict | None = None  # server 블록 위치


@dataclass
class ProxyRoute:
    proxy: str
    environment: str | None
    location: tuple[str, str]
    internal: bool
    upstream: str
    uri: str | None
    target: str | None
    status: str
    subrequests: list[str] = field(default_factory=list)
    settings: list[dict] = field(default_factory=list)
    evidence: dict | None = None  # proxy_pass 지시어 위치


# --- 설정 파일 ----------------------------------------------------------------

def _is_conf_name(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name == "nginx.conf" or name.endswith(CONF_SUFFIXES)


def _parse(snap: Snapshot, rel: str, cache: dict) -> list | None:
    """crossplane으로 한 파일만 읽는다(include는 따라가지 않는다). 실패하면 None."""
    if rel not in cache:
        cache[rel] = None
        try:
            result = crossplane.parse(str(snap.root / rel), single=True, check_ctx=False,
                                      check_args=False, comments=False)
            config = result.get("config") or []
            if result.get("status") == "ok" and config and config[0].get("status") == "ok":
                parsed = config[0].get("parsed")
                cache[rel] = parsed if isinstance(parsed, list) else None
        except Exception:  # crossplane은 입력에 따라 여러 예외를 낸다
            cache[rel] = None
    return cache[rel]


def _walk(nodes):
    for n in nodes or []:
        if isinstance(n, dict):
            yield n
            if isinstance(n.get("block"), list):
                yield from _walk(n["block"])


def _has(nodes, names) -> bool:
    return any(n.get("directive") in names for n in _walk(nodes))


def _config_files(snap: Snapshot, cache: dict) -> list[str]:
    """읽을 수 있는 nginx 이름의 설정 파일 전부(http 설정만 있는 nginx.conf도 포함)."""
    return [rel for rel in snap.files if _is_conf_name(rel) and _parse(snap, rel, cache) is not None]


def _proxy_configs(snap: Snapshot, configs: list[str], cache: dict) -> list[str]:
    return [rel for rel in configs if _has(_parse(snap, rel, cache), PROXY_DIRECTIVES)]


# --- 컨테이너 경로 → 저장소 경로 ---------------------------------------------

def _chain_instrs(df: ParsedArtifact | None) -> list[tuple[str, str, int]]:
    """최종 이미지에 들어가는 명령(마지막 단계와 FROM으로 잇는 앞 단계, 앞 단계부터)."""
    if df is None or not isinstance(df.objects, list):
        return []
    instrs = [x for x in df.objects if isinstance(x, tuple) and len(x) == 3]
    return [x for st in reversed(final_chain(dockerfile_stages(instrs))) for x in st["instrs"]]


def _is_nginx_image(image: str) -> bool:
    """이미지 분류가 nginx 리버스 프록시(knowledge/images.yaml)인가."""
    cls = classify_image(image)
    return bool(cls) and cls["role"] == "reverse-proxy" and cls["component"] == NGINX_COMPONENT


def _final_image(df: ParsedArtifact | None) -> str:
    if df is None or not isinstance(df.objects, list):
        return ""
    stages = dockerfile_stages([x for x in df.objects if isinstance(x, tuple) and len(x) == 3])
    return " ".join(st["image"] for st in final_chain(stages))


def _tokens(arg: str) -> list[str]:
    if arg.startswith("["):
        try:
            data = json.loads(arg)
            return [str(x) for x in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    try:
        return shlex.split(arg)
    except ValueError:
        return arg.split()


def _repo_files_under(snap: Snapshot, src: str) -> list[str] | None:
    """저장소 경로 src가 파일이면 [src], 디렉터리면 그 아래 파일들, 없으면 None."""
    if src == "":
        return list(snap.files)
    if snap.exists(src):
        return [src]
    under = [f for f in snap.files if f.startswith(src + "/")]
    return under or None


def _map_source(snap: Snapshot, src_rel: str, dest: str, out: dict[str, str], is_dir_dest: bool) -> None:
    files = _repo_files_under(snap, src_rel)
    if files is None:
        return
    if files == [src_rel]:
        out[posixpath.join(dest, PurePosixPath(src_rel).name) if is_dir_dest else dest] = src_rel
        return
    for f in files:
        rest = f[len(src_rel) + 1:] if src_rel else f
        out[posixpath.join(dest, rest)] = f


def _norm(path: str) -> str:
    p = posixpath.normpath(path)
    return "" if p == "." else p


def _dockerfile_mapping(snap: Snapshot, df: ParsedArtifact | None, context: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if df is None:
        return out
    # 빌드 컨텍스트 후보: compose build 컨텍스트, Dockerfile 디렉터리, 저장소 루트 순서
    bases = list(dict.fromkeys(b for b in (context, parent_dir(df.path), "") if b is not None))
    workdir = "/"
    for op, arg, _ in _chain_instrs(df):
        if op == "WORKDIR" and arg:
            workdir = posixpath.join(workdir, arg.strip())
        if op not in ("COPY", "ADD"):
            continue
        tokens = _tokens(arg)
        if any(t.startswith("--from") for t in tokens):
            continue
        tokens = [t for t in tokens if not t.startswith("--")]
        if len(tokens) < 2:
            continue
        *sources, dest = tokens
        dest = posixpath.join(workdir, dest)
        is_dir_dest = dest.endswith("/") or len(sources) > 1
        for src in sources:
            if "://" in src:
                continue
            for cand in (_norm(posixpath.join(base, src)) for base in bases):
                if not cand.startswith("..") and _repo_files_under(snap, cand) is not None:
                    _map_source(snap, cand, posixpath.normpath(dest) if not dest.endswith("/") else dest,
                                out, is_dir_dest)
                    break
    return out


def _compose_services(artifacts: list[ParsedArtifact]) -> list[tuple[str, str, dict]]:
    out = []
    for art in artifacts:
        if art.kind != "compose" or not isinstance(art.objects, list):
            continue
        for obj in art.objects:
            if isinstance(obj, tuple) and len(obj) == 2 and isinstance(obj[0], str):
                out.append((art.path, obj[0], as_dict(obj[1])))
    return out


def _compose_mounts(snap: Snapshot, compose_path: str, svc: dict) -> list[tuple[str, str]]:
    """저장소 안을 가리키는 바인드 마운트 (저장소 경로, 컨테이너 경로)."""
    out: list[tuple[str, str]] = []
    volumes = svc.get("volumes")
    for v in volumes if isinstance(volumes, list) else []:
        if isinstance(v, str):
            parts = v.split(":")
            if len(parts) < 2:
                continue
            host, target = parts[0], parts[1]
        elif isinstance(v, dict) and v.get("type", "bind") == "bind":
            host, target = str(v.get("source") or ""), str(v.get("target") or "")
        else:
            continue
        if not host.startswith(".") or not target.startswith("/"):
            continue
        src = _norm(posixpath.join(parent_dir(compose_path), host))
        if not src.startswith(".."):
            out.append((src, target))
    return out


def _compose_mapping(snap: Snapshot, compose_path: str, svc: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for src, target in _compose_mounts(snap, compose_path, svc):
        _map_source(snap, src, target, out, False)
    return out


def _mount_over(snap: Snapshot, image: dict[str, str], compose_path: str, svc: dict) -> dict[str, str]:
    """이미지 매핑 위에 바인드 마운트(docker 동작): 디렉터리 마운트는 그 컨테이너 디렉터리 아래의
    이미지 파일을 모두 가리고, 같은 컨테이너 경로면 마운트가 이긴다."""
    out = dict(image)
    for src, target in _compose_mounts(snap, compose_path, svc):
        if src == "" or not snap.exists(src):  # 디렉터리 마운트
            prefix = posixpath.normpath(target).rstrip("/") + "/"
            out = {c: r for c, r in out.items() if not c.startswith(prefix)}
        _map_source(snap, src, target, out, False)
    return out


def _with_templates(mapping: dict[str, str]) -> dict[str, str]:
    """nginx 공식 이미지: /etc/nginx/templates/X.template → /etc/nginx/conf.d/X."""
    out = dict(mapping)
    for cpath, rel in mapping.items():
        if cpath.startswith(TEMPLATE_DIR) and cpath.endswith(".template"):
            out.setdefault(f"{CONFD_DIR}/{cpath[len(TEMPLATE_DIR):-len('.template')]}", rel)
    return out


# --- 워크로드 환경 변수 --------------------------------------------------------

def _dockerfile_env(df: ParsedArtifact | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for op, arg, _ in _chain_instrs(df):
        if op != "ENV":
            continue
        tokens = _tokens(arg)
        if tokens and "=" in tokens[0]:
            for t in tokens:
                k, sep, v = t.partition("=")
                if sep and k:
                    out[k] = v
        elif tokens:
            out[tokens[0]] = " ".join(tokens[1:])
    return out


def _compose_env(svc: dict) -> dict[str, str]:
    """compose 서비스 `environment`에서 값이 정해진 변수들(값 없는 변수는 호스트 값을 받으므로 없음으로 본다)."""
    return {k: v for k, v in (compose_env(svc.get("environment")) or {}).items() if v is not None}


def _k8s_env(objects: list, w: WorkloadInfo) -> dict[str, str]:
    """워크로드 컨테이너(이미지가 같은 것, 없으면 첫 번째)의 envFrom ConfigMap data,
    그 위에 env[].value(쿠버네티스와 같은 우선순위)."""
    c = workload_container(objects, w)
    if c is None:
        return {}
    configmaps = {as_dict(o.get("metadata")).get("name"): as_dict(o.get("data")) for o in objects
                  if isinstance(o, dict) and o.get("kind") == "ConfigMap"}
    out: dict[str, str] = {}
    env_from = c.get("envFrom")
    for item in env_from if isinstance(env_from, list) else []:
        name = as_dict(as_dict(item).get("configMapRef")).get("name")
        if isinstance(name, str):
            out.update({str(k): str(v) for k, v in configmaps.get(name, {}).items() if v is not None})
    env = c.get("env")
    for item in env if isinstance(env, list) else []:
        item = as_dict(item)
        if isinstance(item.get("name"), str) and "value" in item and item["value"] is not None:
            out[item["name"]] = str(item["value"])
    return out


# --- 지시어 펼치기 -----------------------------------------------------------

def _include_targets(pattern: str, mapping: dict[str, str]) -> list[str]:
    cpath = pattern if pattern.startswith("/") else posixpath.join(NGINX_ROOT, pattern)
    depth = cpath.count("/")
    hits = [c for c in mapping if c.count("/") == depth and fnmatch.fnmatchcase(c, cpath)]
    return [mapping[c] for c in sorted(hits)]


def _expand(snap: Snapshot, rel: str, nodes: list, mapping: dict[str, str], cache: dict,
            stack: tuple[str, ...], included: set[str], budget: list[int]) -> list[dict]:
    """include를 그 자리에 펼친 지시어 트리. 각 지시어에 원래 파일(`file`)을 단다.
    budget(펼칠 수 있는 남은 지시어·include 방문 수)을 다 쓰면 그 뒤의 include는 건너뛴다
    (이미 읽고 있는 파일 자체의 지시어는 남긴다)."""
    out: list[dict] = []
    for n in nodes:
        if not isinstance(n, dict) or not isinstance(n.get("directive"), str):
            continue
        budget[0] -= 1
        args = [str(a) for a in n.get("args") or []]
        if n["directive"] == "include":
            if not args or len(stack) > MAX_INCLUDE_DEPTH:
                continue
            for target in _include_targets(args[0], mapping):
                if budget[0] <= 0:
                    break
                budget[0] -= 1
                parsed = _parse(snap, target, cache) if target not in stack else None
                if parsed is not None:
                    included.add(target)
                    out.extend(_expand(snap, target, parsed, mapping, cache, stack + (target,), included,
                                       budget))
            continue
        node = {"directive": n["directive"], "args": args, "line": n.get("line"), "file": rel, "block": None}
        if isinstance(n.get("block"), list):
            node["block"] = _expand(snap, rel, n["block"], mapping, cache, stack, included, budget)
        out.append(node)
    return out


# --- 설정 값 ------------------------------------------------------------------

def _seconds(raw: str):
    """nginx 시간 값 → 초 단위 정수(1초 미만은 올림)."""
    pos, total_ms = 0, 0
    for m in _TIME.finditer(raw):
        if m.start() != pos:
            return raw
        total_ms += int(m.group(1)) * TIME_UNITS_MS[m.group(2) or "s"]
        pos = m.end()
    return -(-total_ms // 1000) if raw and pos == len(raw) else raw


def _fact(snap: Snapshot, node: dict, key: str, value) -> dict:
    return {"key": key, "value": value, "defaulted": False,
            "evidence": evidence(snap, node["file"], node.get("line"))}


def _block_settings(snap: Snapshot, nodes: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for n in nodes:
        key = n["directive"]
        if key in SETTING_KEYS and n["args"] and key not in out:
            raw = n["args"][0]
            out[key] = _fact(snap, n, key, _seconds(raw) if key in TIME_KEYS else raw)
    return out


def _sorted_facts(facts: dict[str, dict]) -> list[dict]:
    return [facts[k] for k in sorted(facts)]


# --- 호스트 → 워크로드 ---------------------------------------------------------

def _pod_labels(doc: dict) -> dict:
    spec = as_dict(doc.get("spec"))
    if doc.get("kind") == "CronJob":
        spec = as_dict(as_dict(spec.get("jobTemplate")).get("spec"))
    return as_dict(as_dict(as_dict(spec.get("template")).get("metadata")).get("labels"))


@dataclass
class _Scope:
    """한 (프록시 워크로드, 환경)에서 해석에 쓰는 자료."""
    objects: list
    variables: dict[str, str]
    workloads: list[WorkloadInfo]
    compose_names: set[str]
    link_status: str
    hosts: dict[str, str] | None = None  # compose 환경: 서비스 이름 → 대응한 워크로드 id


def _resolve_host(host: str, scope: _Scope) -> tuple[str | None, str]:
    h = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if scope.hosts is not None:  # compose 환경에서는 그 환경의 서비스 이름만 해석한다
        return (scope.hosts[h], scope.link_status) if h in scope.hosts else (None, "candidate")
    return _resolve_k8s_host(h, scope)


def _resolve_k8s_host(h: str, scope: _Scope) -> tuple[str | None, str]:
    by_name = {w.name: w for w in scope.workloads}
    for doc in scope.objects:
        if not isinstance(doc, dict) or doc.get("kind") != "Service" or as_dict(doc.get("metadata")).get("name") != h:
            continue
        selector = as_dict(as_dict(doc.get("spec")).get("selector"))
        if not selector:
            continue
        ids = sorted({w.id for o in scope.objects for w in scope.workloads
                      if is_workload_doc(o, w) and all(_pod_labels(o).get(k) == v for k, v in selector.items())})
        if ids:
            return ids[0], "candidate" if len(ids) > 1 else scope.link_status
    if h in scope.compose_names and h in by_name:
        return by_name[h].id, scope.link_status
    if h in by_name:
        return by_name[h].id, scope.link_status
    return None, "candidate"


def _substitute(text: str, variables: dict[str, str]) -> str:
    return _ENVSUBST.sub(lambda m: variables.get(m.group(1), m.group(0)), text)


def _resolve_pass(arg: str, upstreams: dict[str, dict], scope: _Scope) -> list[dict]:
    """proxy_pass 하나 → 해석된 대상 목록(upstream의 server마다 하나)."""
    m = _PROXY_PASS.match(arg)
    if not m:
        return [{"upstream": arg, "uri": None, "target": None, "status": "candidate", "keepalive": None}]
    raw_host, raw_uri = m.group(1), m.group(2)
    host = _substitute(raw_host, scope.variables)
    uri = _substitute(raw_uri, scope.variables) if raw_uri else None
    if "$" in host:
        return [{"upstream": raw_host, "uri": uri, "target": None, "status": "candidate", "keepalive": None}]
    up = upstreams.get(host)
    hosts = [_substitute(s, scope.variables) for s in up["servers"]] if up else [host]
    out = []
    for h in hosts:
        target, status = (None, "candidate") if "$" in h or not h else _resolve_host(h, scope)
        out.append({"upstream": raw_host, "uri": uri, "target": target, "status": status,
                    "keepalive": up["keepalive"] if up else None})
    return out


# --- server와 location ---------------------------------------------------------

def _location_args(args: list[str]) -> tuple[str, str]:
    if len(args) >= 2:
        return args[0], args[1]
    if not args:
        return "", ""
    if args[0].startswith("@"):
        return "@", args[0][1:]
    m = _MODIFIER.match(args[0])
    return (m.group(1), m.group(2)) if m else ("", args[0])


def _locations(snap: Snapshot, nodes: list[dict], inherited: list[dict[str, dict]], upstreams: dict,
               scope: _Scope, counter: list[int], routes: list[ProxyRoute], proxy: str,
               environment: str | None) -> list[LocationInfo]:
    out: list[LocationInfo] = []
    for n in nodes:
        if n["directive"] != "location" or n["block"] is None:
            continue
        block = n["block"]
        modifier, pattern = _location_args(n["args"])
        loc = LocationInfo(modifier, pattern, any(x["directive"] == "internal" for x in block), counter[0])
        counter[0] += 1
        loc.subrequests = [x["args"][0] for x in block
                           if x["directive"] == "auth_request" and x["args"] and x["args"][0] != "off"]
        chain = [_block_settings(snap, block)] + inherited
        effective: dict[str, dict] = {}
        for level in chain:
            for k, fact in level.items():
                effective.setdefault(k, fact)
        for x in block:
            if x["directive"] != "proxy_pass" or not x["args"]:
                continue
            for r in _resolve_pass(x["args"][0], upstreams, scope):
                loc.proxies.append((r["target"], r["uri"]))
                facts = dict(effective)
                if r["keepalive"] is not None:
                    facts["upstream_keepalive"] = r["keepalive"]
                routes.append(ProxyRoute(
                    proxy, environment, (modifier, pattern), loc.internal, r["upstream"], r["uri"], r["target"],
                    r["status"], list(loc.subrequests), _sorted_facts(facts), evidence(snap, x["file"], x["line"])))
        loc.children = _locations(snap, block, chain, upstreams, scope, counter, routes, proxy, environment)
        out.append(loc)
    return out


def _http_nodes(nodes: list[dict]) -> list[dict]:
    """http 문맥 지시어: 파일 맨 위(conf.d 파일은 http 안에 include된다)와 http 블록 안."""
    out = []
    for n in nodes:
        if n["directive"] == "http" and n["block"] is not None:
            out.extend(n["block"])
        else:
            out.append(n)
    return out


def _upstreams(snap: Snapshot, trees: list[list[dict]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for nodes in trees:
        for n in _http_nodes(nodes):
            if n["directive"] != "upstream" or not n["args"] or n["block"] is None or n["args"][0] in out:
                continue
            servers = [x["args"][0] for x in n["block"] if x["directive"] == "server" and x["args"]]
            keep = next((x for x in n["block"] if x["directive"] == "keepalive" and x["args"]), None)
            value = int(keep["args"][0]) if keep and keep["args"][0].isdigit() else None
            out[n["args"][0]] = {"servers": servers,
                                 "keepalive": _fact(snap, keep, "upstream_keepalive", value) if value is not None else None}
    return out


# --- 설정 ↔ 워크로드 연결 -------------------------------------------------------

def _is_confd_entry(cpath: str | None) -> bool:
    return cpath is not None and posixpath.dirname(cpath) == CONFD_DIR and cpath.endswith(".conf")


def _scope_mapping(snap: Snapshot, w: WorkloadInfo, env: Environment | None, image: dict[str, str],
                   services: list[tuple[str, str, dict]]) -> dict[str, str]:
    """한 범위에서 컨테이너 경로 → 저장소 경로: 이미지(Dockerfile COPY/ADD) 위에 그 범위의 바인드 마운트.
    compose 환경에서는 런타임 마운트가 이미지 내용을 덮고 디렉터리 마운트는 그 아래 이미지 파일을 가린다(docker 동작)."""
    mapping = dict(image)
    if env is None:
        if w.source == "compose":
            for path, name, svc in services:
                if name == w.name:
                    mapping.update(_compose_mapping(snap, path, svc))
    else:
        svc = env_service(env, w)
        if svc is not None:
            mapping = _mount_over(snap, mapping, env.source, svc)
    return _with_templates(mapping)


def _links(workloads: list[WorkloadInfo], configs: list[str], proxy_configs: list[str],
           dockerfiles: dict[str, ParsedArtifact | None],
           mappings: dict[tuple[str, str | None], dict[str, str]]) -> dict[tuple[str, str | None], list]:
    """(워크로드 id, 환경 이름) → [(저장소 파일, 컨테이너 경로 또는 None, 연결 상태)].
    컨테이너에 들어간 설정은 프록시 지시어가 없어도(http 설정만 있는 nginx.conf 등) 연결하고,
    어느 범위에도 들어가지 않은 설정을 하나뿐인 nginx 워크로드에 붙이는 대체 규칙은 프록시 지시어가 있는 설정만 쓴다."""
    nginx_based = [w for w in workloads
                   if any(_is_nginx_image(i) for i in [w.image] + _final_image(dockerfiles[w.id]).split())]
    nginx_ids = {w.id for w in nginx_based}
    links: dict[tuple[str, str | None], list[tuple[str, str | None, str]]] = {key: [] for key in mappings}
    linked: set[str] = set()
    for key, mapping in mappings.items():
        for cpath, rel in sorted(mapping.items()):
            # nginx 기반이 아닌 워크로드는 /etc/nginx/ 아래로 들어간 설정만 연결한다(`COPY . .` 등 제외)
            if rel in configs and (key[0] in nginx_ids or cpath.startswith(NGINX_ROOT)):
                links[key].append((rel, cpath, "confirmed"))
                linked.add(rel)
    if len(nginx_based) == 1:
        for key in links:
            if key[0] == nginx_based[0].id:
                links[key].extend((rel, None, "candidate") for rel in proxy_configs if rel not in linked)
    return links


def _entries(snap: Snapshot, links: list[tuple[str, str | None, str]], cache: dict) -> list[tuple[str, str]]:
    # /etc/nginx/nginx.conf가 들어가 있으면 그것 하나가 진입점이다(conf.d는 그 include로 들어오고 http 설정도 거기 있다)
    entries = [(rel, status) for rel, cpath, status in links if cpath == NGINX_MAIN]
    if not entries:
        entries = [(rel, status) for rel, cpath, status in links if _is_confd_entry(cpath)]
    if not entries:
        entries = [(rel, status) for rel, _, status in links if _has(_parse(snap, rel, cache), {"server"})]
    seen, out = set(), []
    for rel, status in entries:
        if rel not in seen:
            seen.add(rel)
            out.append((rel, status))
    return out


def find_proxies(snap: Snapshot, workloads: list[WorkloadInfo], artifacts: list[ParsedArtifact],
                 environments: list[Environment]) -> tuple[list[ProxyServer], list[ProxyRoute]]:
    cache: dict = {}
    configs = _config_files(snap, cache)
    proxy_configs = _proxy_configs(snap, configs, cache)
    if not proxy_configs:
        return [], []
    dockerfiles = {w.id: workload_dockerfile(w, artifacts) for w in workloads}
    services = _compose_services(artifacts)
    scopes = {w.id: env_scopes(w, environments) for w in workloads}
    mappings: dict[tuple[str, str | None], dict[str, str]] = {}
    for w in workloads:
        image = _dockerfile_mapping(snap, dockerfiles[w.id], w.build_context)
        for env in scopes[w.id]:
            mappings[(w.id, env.name if env else None)] = _scope_mapping(snap, w, env, image, services)
    links = _links(workloads, configs, proxy_configs, dockerfiles, mappings)
    raw_k8s = [d for a in artifacts if a.kind == "k8s" and not is_build_path(a.path) and isinstance(a.objects, list)
               for d in a.objects if isinstance(d, dict)]
    compose_names = {name for _, name, _ in services}
    servers: list[ProxyServer] = []
    routes: list[ProxyRoute] = []
    for w in workloads:
        for env in scopes[w.id]:
            env_name = env.name if env else None
            key = (w.id, env_name)
            entries = _entries(snap, links[key], cache)
            if not entries:
                continue
            trees, included = [], set()
            for rel, status in entries:
                trees.append((rel, status, _expand(snap, rel, _parse(snap, rel, cache) or [], mappings[key], cache,
                                                   (rel,), included, [MAX_EXPANDED])))
            # 다른 진입 설정이 include한 파일은 따로 진입점으로 세지 않는다
            trees = [t for t in trees if t[0] not in included]
            upstreams = _upstreams(snap, [t[2] for t in trees])
            variables = _dockerfile_env(dockerfiles[w.id])
            hosts = None
            if env is None:
                for _, name, svc in services:
                    if name == w.name:
                        variables.update(_compose_env(svc))
                objects = raw_k8s
                variables.update(_k8s_env(objects, w))
            elif env.kind == "compose":
                objects = []
                variables.update(_compose_env(env_service(env, w) or {}))
                hosts = {name: wid for wid, name in env.members.items()}
            else:
                objects = env.objects
                variables.update(_k8s_env(objects, w))
            for rel, status, nodes in trees:
                scope = _Scope(objects, variables, workloads, compose_names, status, hosts)
                http = _http_nodes(nodes)
                http_settings = _block_settings(snap, http)
                counter = [0]
                for n in http:
                    if n["directive"] != "server" or n["block"] is None:
                        continue
                    server_settings = _block_settings(snap, n["block"])
                    effective = {**http_settings, **server_settings}
                    counter[0] = 0
                    locs = _locations(snap, n["block"], [server_settings, http_settings], upstreams, scope,
                                      counter, routes, w.id, env_name)
                    servers.append(ProxyServer(w.id, env_name, _sorted_facts(effective), locs,
                                               evidence(snap, n["file"], n["line"])))
    servers.sort(key=lambda s: (s.proxy, s.environment or "", s.evidence["path"], s.evidence["line"] or 0))
    routes.sort(key=lambda r: (r.proxy, r.environment or "", r.evidence["path"], r.evidence["line"] or 0,
                               r.location, r.upstream, r.target or "", r.uri or ""))
    return servers, routes
