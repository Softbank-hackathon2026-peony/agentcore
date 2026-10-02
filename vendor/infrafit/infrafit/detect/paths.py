"""요청 경로: 엣지 → 로드밸런서 → 리버스 프록시 → 앱 서버 (설계 §6.6)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from infrafit import kb
from infrafit.detect.artifacts import ParsedArtifact, as_dict, build_source, ingress_backends
from infrafit.detect import proxy_graph
from infrafit.detect.components import platform_config_for
from infrafit.detect.defaults import fact_settings, hop_settings
from infrafit.detect.environments import Environment, env_command, env_command_evidence, env_scopes, env_slug
from infrafit.detect.jvm import jvm_build_dirs, owning_build_dir
from infrafit.detect.nginx import NGINX_COMPONENT, ProxyRoute, ProxyServer
from infrafit.detect.shell import shell_tokens, simple_commands
from infrafit.detect.testpaths import is_test_path
from infrafit.detect.workloads import WorkloadInfo
from infrafit.evidence import evidence
from infrafit.repo import Snapshot

EDGE_BY_FILE = {
    "vercel.json": "nw:vercel/edge-proxy/default",
    "netlify.toml": "nw:netlify/edge-proxy/default",
    "fly.toml": "nw:fly/proxy/default",
    "render.yaml": "nw:render/proxy/default",
    "railway.json": "nw:railway/proxy/default",
    "railway.toml": "nw:railway/proxy/default",
    "railway.ts": "nw:railway/proxy/default",
}
LB_BY_CLASS = {"alb": "nw:aws/alb/default", "gce": "nw:gcp/classic-alb/gke-ingress", "nginx": "nw:k8s/ingress-nginx/default"}
MANAGED_RUNTIME_PREFIXES = ("cp:vercel/", "cp:netlify/")
_ALB_IDLE = re.compile(r"idle_timeout\.timeout_seconds=(\d+)")
SPRING_TOMCAT = "nw:app/spring-boot-tomcat/default"
SPRING_CONFIG_GLOBS = ("**/application*.yml", "**/application*.yaml", "**/application*.properties")
# Tomcat keep-alive 설정 키(완화 바인딩: 소문자, `-`·`_` 뺌). 앞의 키가 우선이다
TOMCAT_KEEP_ALIVE_KEYS = ("server.tomcat.keepalivetimeout", "server.tomcat.connectiontimeout")
_YAML_KEY = re.compile(r"^(\s*)([\"']?)([\w.\-\[\]]+)\2\s*:(?:\s+(.*))?$")
_PROPERTIES_KEY = re.compile(r"^\s*([\w.\-\[\]]+)\s*[=:]\s*(.*?)\s*$")
_DURATION_UNITS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _server_of(tokens: list[str]) -> tuple[str, dict] | None:
    """단순 명령 하나가 띄우는 앱 서버."""
    names = [PurePosixPath(t).name for t in tokens]
    text = " ".join(tokens)
    if "uvicorn" in names:
        m = re.search(r"--timeout-keep-alive[ =](\d+)", text)
        return "nw:app/uvicorn/default", ({"timeout_keep_alive": int(m.group(1))} if m else {})
    if "gunicorn" in names:
        m = re.search(r"--keep-?alive[ =](\d+)", text)
        return "nw:app/gunicorn/default", ({"keepalive": int(m.group(1))} if m else {})
    if any(a == "next" and b == "start" for a, b in zip(names, names[1:])):
        return "nw:app/next-start/default", {}
    if "flask" in names and "run" in names[names.index("flask") + 1:]:
        return "nw:app/flask-dev/default", {}
    if "manage.py" in names and "runserver" in names:
        return "nw:app/django-runserver/default", {}
    if names[0] == "node" or names[:2] == ["npm", "start"]:
        return "nw:app/node-http/default", {}
    return None


def app_server(command: str) -> tuple[str, dict] | None:
    """실행 명령이 띄우는 앱 서버(구성 요소, 명시 설정). 단순 명령마다 보고 마지막으로 알아본 서버를 쓴다."""
    cmd = command.strip() if isinstance(command, str) else ""
    found = [server for tokens in simple_commands(shell_tokens(cmd)) if (server := _server_of(tokens))]
    return found[-1] if found else None


def _config_entries(snap: Snapshot, rel: str):
    """Spring 설정 파일의 (점으로 이은 키, 값, 줄). YAML은 들여쓰기로 키 경로를 만든다(목록 항목은 건너뛴다)."""
    if rel.endswith(".properties"):
        for i, text in enumerate(snap.lines(rel), 1):
            m = _PROPERTIES_KEY.match(text)
            if m and not text.lstrip().startswith(("#", "!")):
                yield m.group(1), m.group(2), i
        return
    stack: list[tuple[int, str]] = []
    for i, text in enumerate(snap.lines(rel), 1):
        if text.strip() == "---":  # 다음 YAML 문서
            stack = []
            continue
        m = _YAML_KEY.match(text)
        if not m or text.lstrip().startswith("#"):
            continue
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        value = (m.group(4) or "").split(" #", 1)[0].strip().strip("\"'")
        if value:
            yield ".".join([k for _, k in stack] + [m.group(3)]), value, i
        else:
            stack.append((indent, m.group(3)))


def _seconds(value: str) -> int | float | None:
    """Spring Duration 값 → 초: `20s`·`2m` 등 단위 붙은 값, `PT20S`(ISO-8601), 단위 없는 수는 밀리초."""
    v = value.strip()
    if re.fullmatch(r"\d+", v):
        seconds = int(v) / 1000
    elif m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ns|us|ms|s|m|h|d)", v, re.IGNORECASE):
        seconds = float(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]
    elif m := re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", v, re.IGNORECASE):
        if not any(m.groups()):
            return None
        seconds = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
    else:
        return None
    return int(seconds) if float(seconds).is_integer() else seconds


def _spring_configs(snap: Snapshot, root: str) -> list[str]:
    """모듈 code_root의 application*.yml|yaml|properties(테스트 제외): 파일을 품은 가장 가까운 빌드 파일 모듈이
    code_root인 것만(하위 모듈의 설정은 그 모듈의 것이다). 기본 파일(`application.*`)이 프로필 파일보다 먼저."""
    build_dirs = set(jvm_build_dirs(snap))
    rels = {rel for pattern in SPRING_CONFIG_GLOBS for rel in snap.glob(pattern)
            if not is_test_path(rel) and owning_build_dir(rel, build_dirs) == root}
    return sorted(rels, key=lambda rel: (PurePosixPath(rel).stem != "application", rel))


def _tomcat_keep_alive(snap: Snapshot, root: str) -> tuple[int | float, dict] | None:
    """Tomcat keep-alive 명시값(초)과 그 줄 근거. 키 우선순위 → 파일 순서로 처음 읽을 수 있는 값."""
    configs = _spring_configs(snap, root)
    entries = [(re.sub(r"[-_]", "", key.lower()), value, rel, line)
               for rel in configs for key, value, line in _config_entries(snap, rel)]
    for wanted in TOMCAT_KEEP_ALIVE_KEYS:
        for key, value, rel, line in entries:
            seconds = _seconds(value) if key == wanted else None
            if seconds is not None:
                return seconds, evidence(snap, rel, line)
    return None


def _spring_hop(snap: Snapshot, w: WorkloadInfo) -> dict:
    """Spring 워크로드의 앱 서버 구간: spring-mvc는 명령과 무관하게 내장 Tomcat(설정의 keep-alive 명시값이 있으면
    그 값과 줄 근거, 없으면 기본값과 웹 의존성 줄 근거), spring-webflux는 unmapped."""
    dep_ev = [w.framework_evidence or w.entrypoint]
    if w.framework != "spring-mvc":
        return _hop("app-server", "unmapped", {}, dep_ev)
    found = _tomcat_keep_alive(snap, w.code_root or "")
    if found is None:
        return _hop("app-server", SPRING_TOMCAT, {}, dep_ev)
    seconds, ev = found
    hop = _hop("app-server", SPRING_TOMCAT, {"keep_alive_timeout": seconds}, [ev])
    for setting in hop["settings"]:
        if not setting["defaulted"]:
            setting["evidence"] = ev
    return hop


def _hop(kind: str, component: str, explicit: dict, ev: list[dict]) -> dict:
    return {"order": 0, "kind": kind, "component": component, "osi_layer": "L7",
            "settings": hop_settings(component, explicit, kb.defaults()), "evidence": ev}


def _edge(snap: Snapshot, w: WorkloadInfo, artifacts: list[ParsedArtifact]) -> dict | None:
    """이 워크로드의 컴퓨트를 정한 플랫폼 설정이 있을 때만 그 플랫폼의 엣지를 지난다."""
    config = platform_config_for(w, artifacts)
    comp = EDGE_BY_FILE.get(PurePosixPath(config.path).name) if config else None
    return _hop("edge-proxy", comp, {}, [evidence(snap, config.path)]) if comp else None


def _ingress_hop(snap: Snapshot, w: WorkloadInfo, objects: list, source: str) -> dict | None:
    for doc in objects if isinstance(objects, list) else []:
        if not isinstance(doc, dict) or doc.get("kind") != "Ingress" or w.name not in ingress_backends(doc):
            continue
        annotations = as_dict(as_dict(doc.get("metadata")).get("annotations"))
        cls = as_dict(doc.get("spec")).get("ingressClassName") or annotations.get("kubernetes.io/ingress.class")
        comp = LB_BY_CLASS.get(str(cls), "unmapped")
        explicit = {}
        m = _ALB_IDLE.search(str(annotations.get("alb.ingress.kubernetes.io/load-balancer-attributes", "")))
        if comp == "nw:aws/alb/default" and m:
            explicit["idle_timeout"] = int(m.group(1))
        # 렌더 결과(#build)는 실제 파일이 아니므로 kustomization.yaml을 근거로 쓴다
        return _hop("load-balancer", comp, explicit, [evidence(snap, build_source(source))])
    return None


def _load_balancer(snap: Snapshot, w: WorkloadInfo, artifacts: list[ParsedArtifact]) -> dict | None:
    # 경로 순으로 보아 실제 파일이 kustomize 렌더(#build)보다 먼저 나오게 한다.
    for art in sorted(artifacts, key=lambda a: a.path):
        if art.kind != "k8s":
            continue
        hop = _ingress_hop(snap, w, art.objects, art.path)
        if hop:
            return hop
    return None


def _proxy_hop(groups: list[list[dict]], evs: list[dict | None]) -> dict:
    """리버스 프록시 구간: 설정은 route·server마다 기본값을 채운 사실을 값마다 하나씩(같은 키라도) 둔다."""
    seen, ev = set(), []
    for e in evs:
        if e and (e["path"], e["line"]) not in seen:
            seen.add((e["path"], e["line"]))
            ev.append(e)
    ev.sort(key=lambda e: (e["path"], e["line"] or 0))
    return {"order": 0, "kind": "reverse-proxy", "component": NGINX_COMPONENT, "osi_layer": "L7",
            "settings": fact_settings(NGINX_COMPONENT, groups, kb.defaults()), "evidence": ev}


def _front(snap: Snapshot, w: WorkloadInfo, artifacts: list[ParsedArtifact], env: Environment | None) -> list[dict]:
    """워크로드의 앞 구간(엣지·로드밸런서). env가 None이면 환경 밖 매니페스트에서 찾는다.
    compose 환경에는 앞 구간이 없다."""
    if env is not None and env.kind == "compose":
        return []
    lb = _load_balancer(snap, w, artifacts) if env is None else _ingress_hop(snap, w, env.objects, env.source)
    return [h for h in (_edge(snap, w, artifacts), lb) if h]


def fronted_proxies(snap: Snapshot, workloads: list[WorkloadInfo], artifacts: list[ParsedArtifact],
                    environments: list[Environment], servers: list[ProxyServer]) -> set[tuple[str, str | None]]:
    """자기 앞 구간(엣지·로드밸런서)이 있는 (프록시 워크로드 id, 환경 이름). 체인은 이런 프록시에서 끝난다."""
    return proxy_graph.fronted_proxies(workloads, environments, servers,
                                       lambda w, e: bool(_front(snap, w, artifacts, e)))


def build_paths(snap: Snapshot, workloads: list[WorkloadInfo], artifacts: list[ParsedArtifact],
                compute: dict[str, str], environments: list[Environment],
                proxy: tuple[list[ProxyServer], list[ProxyRoute]] | None = None) -> list[dict]:
    """워크로드마다, 그 워크로드가 있는 렌더된 환경마다 경로 하나. 어느 환경에도 없으면 environment null 경로 하나.
    nginx 프록시 워크로드도 경로를 갖고, 자기 앞 구간이 없는 프록시 대상은 프록시를 거치는 경로를 갖는다.
    nginx 해석이 없는 reverse-proxy 워크로드는 앞 구간 + 이미지 분류의 프록시 구간(설정은 기본값만)이다."""
    servers, routes = proxy if proxy else ([], [])
    proxy_ids = {s.proxy for s in servers}
    by_id = {w.id: w for w in workloads}
    fronted = fronted_proxies(snap, workloads, artifacts, environments, servers)
    paths = []
    for w in sorted(workloads, key=lambda w: w.id):
        static = w.kind == "static-frontend" and w.id not in proxy_ids
        # 정적 프런트엔드는 플랫폼 설정이 정한 엣지가 있을 때만 경로(엣지 구간 하나)를 갖는다
        if static and _edge(snap, w, artifacts) is None:
            continue
        if not static and w.kind not in ("web", "reverse-proxy") and w.id not in proxy_ids:
            continue
        has_app_server = (not static and w.id not in proxy_ids
                          and not str(compute.get(w.id, "")).startswith(MANAGED_RUNTIME_PREFIXES))

        def hops_for(env: Environment | None) -> list[dict]:
            env_name = env.name if env else None
            # 앱 서버 구간은 그 환경에서 실제로 실행하는 명령으로 정한다
            server = app_server(env_command(env, w, artifacts)) if has_app_server else None
            tail = ([_hop("app-server", server[0], server[1], env_command_evidence(snap, env, w, artifacts))]
                    if server else [])
            if has_app_server and w.framework.startswith("spring-"):  # Spring은 명령과 무관하게 정한다
                tail = [_spring_hop(snap, w)]
            if w.id in proxy_ids:
                mine = [s for s in servers if s.proxy == w.id and s.environment == env_name]
                # 이 환경의 server가 없어도 프록시 구간은 기본값만으로 둔다
                tail = [_proxy_hop([s.settings for s in mine] or [[]], [s.evidence for s in mine])]
            own = _front(snap, w, artifacts, env)
            if w.kind == "reverse-proxy" and w.id not in proxy_ids:
                comp = w.proxy_component or "unmapped"
                return own + [_hop("reverse-proxy", comp, {}, [w.entrypoint])]
            if own:
                return own + tail
            # 앞 구간이 없으면 이 워크로드로 넘기는 프록시 체인을 따라 올라간다(단계마다 프록시 id 순 첫 번째)
            env_routes = [r for r in routes if r.environment == env_name and r.proxy in by_id]
            chain = proxy_graph.first_chain(env_routes, proxy_graph.fronted_in(fronted, env_name), w.id)
            if not chain:
                return tail
            hops = [_proxy_hop([r.settings for r in mine], [r.evidence for r in mine]) for _, mine in chain]
            return _front(snap, by_id[chain[0][0]], artifacts, env) + hops + tail

        def make(pid: str, env: Environment | None) -> dict:
            hops = [dict(h) for h in hops_for(env)]
            for i, hop in enumerate(hops):
                hop["order"] = i
            return {"id": pid, "workload": w.id, "environment": env.name if env else None, "hops": hops}

        for e in env_scopes(w, sorted(environments, key=lambda e: e.name)):
            paths.append(make(f"path-{w.id[2:]}.{env_slug(e.name)}" if e else f"path-{w.id[2:]}", e))
    return paths
