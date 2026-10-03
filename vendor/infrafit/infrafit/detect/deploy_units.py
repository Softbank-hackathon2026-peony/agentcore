"""배포 단위(deploy_units): 프로젝트가 스스로 정의한 "같이 떠야 하는 컨테이너 묶음"(다중 컨테이너 계약 §1).
출처 우선순위는 compose(기본 파일 + override) → k8s → 코드에서 찾은 워크로드다. 정하지 못한 값은 지어내지 않고
`unresolved`에 적는다."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

from infrafit import kb
from infrafit.detect.artifacts import (ParsedArtifact, as_dict, build_source, dockerfile_at, ingress_backends,
                                       is_build_path, pod_spec)
from infrafit.detect.compose import compose_build, compose_variant, depends_on, service_line
from infrafit.detect.components import IMAGE_SCOPE_ROLES, scope_id
from infrafit.detect.environments import (Environment, compose_env, compose_environments, compose_key_line,
                                          match_services)
from infrafit.detect.images import image_class, image_name
from infrafit.detect.signatures import is_aux_path
from infrafit.detect.testpaths import is_test_path
from infrafit.detect.workloads import (WorkloadInfo, compose_command, compose_kind, dockerfile_for_image,
                                       dockerfile_workload_name, is_app, is_worker, recovered_context, slug,
                                       workload_dockerfile)
from infrafit.evidence import evidence, line_of
from infrafit.repo import Snapshot, parent_dir

STORE_ROLES = ("datastore", "cache", "queue")  # 이미지 분류 role 중 데이터 저장소 컨테이너
ENTRY_KINDS = ("web", "reverse-proxy")
K8S_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")
ONE_SHOT_KINDS = ("migration-job", "batch")  # 코드에서 찾은 워크로드 중 실행하고 끝나는 것
LOOPBACK_HOSTS = ("127.", "localhost", "[::1]", "::1")
# 실행 명령이 듣는 포트: `--port 8000`, `--port=8000`, `--bind 0.0.0.0:8000`, `-b :8000`
_CMD_PORT = re.compile(r"(?:^|\s)(?:--port|--bind|-b)[ =](?:\S*:)?(\d{2,5})(?=\s|$)")
_INTERPOLATION = re.compile(r"\$\{?[A-Za-z_]")  # 실행하는 호스트의 환경변수로 채우는 값
# 코드에서 저장소 주소를 기본값으로 읽는 환경변수: os.environ.get("X", "redis://…"), os.getenv(…), process.env.X || "…".
# 기본값 URL 은 (스킴, 호스트, 포트, 경로)까지 잡는다(사용자 정보는 건너뛴다)
_URL_DEFAULT = r"""(\w+)://(?:[^/@\s"'`]*@)?(\[[^\]\s]*\]|[^/:\s"'`@]+)(?::(\d+))?(/[^\s"'`]*)?"""
_ENV_DEFAULT = (
    re.compile(r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']\s*,\s*["']""" + _URL_DEFAULT),
    re.compile(r"""process\.env\.([A-Z][A-Z0-9_]*)\s*(?:\|\||\?\?)\s*["'`]""" + _URL_DEFAULT),
)
CODE_EXTS = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")


def _unresolved(out: list[dict], field: str, why: str) -> None:
    item = {"field": field, "why": why}
    if item not in out:
        out.append(item)


# --- 공통 규칙 ------------------------------------------------------------------

def is_secret(key: str, value: str | None) -> bool:
    """비밀값인가: 키 이름이나 값이 knowledge/secrets.yaml 정규식에 맞는다."""
    rules = kb.secrets()
    if any(re.search(rx, key, re.IGNORECASE) for rx in rules["key"]):
        return True
    return value is not None and any(re.search(rx, value) for rx in rules["value"])


def _env_map(raw) -> dict[str, str | None]:
    """compose `environment`(목록·맵) → 이름 → 값."""
    return compose_env(raw) or {}


def _split_env(env: dict[str, str | None]) -> tuple[dict[str, str], list[str]]:
    """(내보내도 되는 값, 모든 이름). 비밀값, 호스트에서 받는 값(값 없음·`${X}`)은 이름만 남긴다."""
    values = {k: v for k, v in env.items()
              if v is not None and not _INTERPOLATION.search(v) and not is_secret(k, v)}
    return dict(sorted(values.items())), sorted(env)


def _command_text(value) -> str:
    """명령을 적힌 대로 문자열로: 문자열은 그대로, 목록은 셸 규칙으로 이어 붙인다(다시 나눠도 같은 목록이 된다)."""
    if isinstance(value, list):
        return shlex.join(str(x) for x in value)
    return value if isinstance(value, str) else ""


def _command_port(command: str) -> list[int]:
    return [int(m.group(1)) for m in _CMD_PORT.finditer(command or "")][:1]


def _expose_ports(df: ParsedArtifact | None) -> list[int]:
    """Dockerfile 최종 이미지의 EXPOSE 포트(`8000 9100`, `8080/tcp`). 변수로 적힌 것은 뺀다."""
    text = str(df.get("expose") or "") if df else ""
    return [int(p) for p in (t.split("/")[0] for t in text.split()) if p.isdigit()]


def _dockerfile_evidence(snap: Snapshot, df: ParsedArtifact) -> dict:
    """Dockerfile 이미지의 근거: 최종 단계 FROM 줄, 없으면 파일."""
    fact = next((f for f in df.settings if f["key"] == "base_image" and f.get("evidence")), None)
    return fact["evidence"] if fact else evidence(snap, df.path)


def _add_image(images: list[dict], keys: dict[tuple, str], key: tuple, name: str, ev: dict,
               snap: Snapshot, unresolved: list[dict]) -> str:
    """빌드(컨텍스트, Dockerfile, target)마다 이미지 하나. 같은 빌드를 쓰는 컨테이너는 id를 공유한다."""
    if key in keys:
        return keys[key]
    context, dockerfile, target = key
    base = name or "app"
    iid = base if all(i["id"] != base for i in images) else f"{base}-{len(images) + 1}"
    keys[key] = iid
    item = {"id": iid, "context": context}
    if dockerfile and snap.exists(dockerfile):
        item["dockerfile"] = dockerfile
    else:
        _unresolved(unresolved, f"images.{iid}.dockerfile",
                    f"Dockerfile {dockerfile}이 저장소에 없다" if dockerfile else "Dockerfile이 없다")
    if target:
        item["target"] = target
    item["evidence"] = ev
    images.append(item)
    return iid


def _store_link(cls: dict, datastore_ids: set[str]) -> str | None:
    """저장소 컨테이너 ↔ inventory datastores: 이미지 분류 구성 요소로 만드는 범위 id(components.scope_id)."""
    role = IMAGE_SCOPE_ROLES.get(cls["role"])
    if not cls.get("component") or not role:
        return None
    sid = scope_id(cls["component"], role)
    return sid if sid in datastore_ids else None


def _pick_entry(cands: list[tuple[str, str, int]], unresolved: list[dict], what: str) -> dict | None:
    """(컨테이너, 종류, 포트) 후보 중 entry: 리버스 프록시 우선. 후보가 없으면 None. 같은 순위가 여럿이면
    정의 순서 첫 번째를 고르고 unresolved에 남긴다(사용자 확인)."""
    if not cands:
        return None
    top = [c for c in cands if c[1] == "reverse-proxy"] or cands
    if len(top) > 1:
        why = f"{what}가 여럿({', '.join(c[0] for c in top)}): 정의 순서 첫 번째"
        _unresolved(unresolved, "entry", f"{why}를 골랐다. 사용자 확인 필요")
    elif len(cands) > 1:
        why = f"{what} 중 하나뿐인 프록시"
    else:
        why = f"유일하게 {what}"
    return {"container": top[0][0], "port": top[0][2], "why": why}


# --- compose -------------------------------------------------------------------

def _main_compose(artifacts: list[ParsedArtifact]) -> Environment | None:
    """배포 단위로 읽을 compose 설정: 기본 파일(+ 같은 계열 override) 환경 중 저장소 루트에 가장 가까운 것.
    기본 파일이 없으면 첫 변형 파일 환경."""
    envs = compose_environments(artifacts)
    base = [e for e in envs if compose_variant(e.source) == ""]
    pool = base or envs
    if not pool:
        return None
    return min(pool, key=lambda e: (len(PurePosixPath(parent_dir(e.source)).parts), e.source))


def _compose_where(snap: Snapshot, env: Environment, name: str) -> tuple[str, int | None]:
    """서비스 정의 위치: 기본 파일, 없으면 그 서비스를 정한 다른 파일."""
    files = [env.source] + sorted(set(env.origins.get(name, {}).values()) - {env.source})
    for rel in files:
        line = service_line(snap, rel, name)
        if line:
            return rel, line
    return env.source, None


def _compose_ports(svc: dict, unresolved: list[dict], field: str) -> tuple[list[int], list[int]]:
    """(컨테이너 포트, 호스트에 공개한 컨테이너 포트). `ports`는 `[호스트:]컨테이너[/프로토콜]`과 긴 형식,
    `expose`는 컨테이너 포트만. 루프백 주소에만 묶은 포트는 공개가 아니다. 범위·변수는 읽지 않는다."""
    ports: list[int] = []
    published: list[int] = []
    raw = svc.get("ports") if isinstance(svc.get("ports"), list) else []
    for p in raw:
        if isinstance(p, dict):
            target, host = str(p.get("target") or ""), str(p.get("host_ip") or "")
        else:
            text = str(p).split("/")[0]
            host, _, target = text.rpartition(":")
        if not target.isdigit():
            _unresolved(unresolved, field, f"읽을 수 없는 포트 표기 {p!r}")
            continue
        port = int(target)
        if port not in ports:
            ports.append(port)
        if not host.startswith(LOOPBACK_HOSTS) and port not in published:
            published.append(port)
    for p in svc.get("expose") if isinstance(svc.get("expose"), list) else []:
        text = str(p).split("/")[0]
        if text.isdigit() and int(text) not in ports:
            ports.append(int(text))
    return ports, published


def _awaited(services: dict[str, dict]) -> set[str]:
    """다른 서비스가 `condition: service_completed_successfully`로 끝나기를 기다리는 서비스."""
    out: set[str] = set()
    for svc in services.values():
        deps = as_dict(svc).get("depends_on")
        for name, cond in (deps.items() if isinstance(deps, dict) else []):
            if as_dict(cond).get("condition") == "service_completed_successfully":
                out.add(str(name))
    return out


def _referenced_port(name: str, services: dict[str, dict]) -> list[int]:
    """다른 서비스의 환경변수 값이 이 서비스에 접속하는 포트(`@postgres:5432`, `redis://redis:6379`)."""
    rx = re.compile(rf"(?:@|//){re.escape(name)}:(\d{{2,5}})\b")
    for other, svc in services.items():
        if other == name:
            continue
        for value in _env_map(as_dict(svc).get("environment")).values():
            if value and (m := rx.search(value)):
                return [int(m.group(1))]
    return []


def _from_compose(snap: Snapshot, artifacts: list[ParsedArtifact], workloads: list[WorkloadInfo],
                  datastore_ids: set[str]) -> dict | None:
    env = _main_compose(artifacts)
    if env is None:
        return None
    match_services(env, workloads, artifacts)
    linked = {svc: wid for wid, svc in env.members.items()}
    unresolved: list[dict] = []
    where = {n: _compose_where(snap, env, n) for n in env.services}
    # 정의 순서(파일, 줄)대로 본다. profiles가 있는 서비스는 `docker compose up`이 띄우지 않으므로 뺀다
    names = sorted((n for n in env.services if not as_dict(env.services[n]).get("profiles")),
                   key=lambda n: (where[n][0] != env.source, where[n][0], where[n][1] or 0, n))
    awaited = _awaited(env.services)
    images: list[dict] = []
    keys: dict[tuple, str] = {}
    containers: list[dict] = []
    stores: list[dict] = []
    entry_cands: list[tuple[str, str, int]] = []
    kept: list[str] = []
    for name in names:
        svc = as_dict(env.services[name])
        build = compose_build(env.source, svc)
        df = dockerfile_at(build[1], artifacts) if build else None
        image = svc.get("image") if isinstance(svc.get("image"), str) else ""
        cls = image_class(image, df)
        role = cls["role"] if cls else None
        if role == "dev-tool":  # 개발 도구(adminer 등)는 배포 대상이 아니다
            continue
        kept.append(name)
        ev = evidence(snap, *where[name])
        env_values, env_names = _split_env(_env_map(svc.get("environment")))
        if svc.get("env_file"):
            _unresolved(unresolved, f"containers.{name}.env", "env_file 값은 읽지 않는다(비밀값 파일일 수 있다)")
        ports, published = _compose_ports(svc, unresolved, f"containers.{name}.ports")
        image_id = None
        if build:
            raw_build = svc.get("build")
            context = str(as_dict(raw_build).get("context") or raw_build or "")
            if "://" in context or build[0].startswith(".."):
                _unresolved(unresolved, f"containers.{name}.image", f"저장소 밖 빌드 컨텍스트 {context}")
            else:
                iname = image_name(image) or dockerfile_workload_name(build[1])
                rel = env.origins.get(name, {}).get("build", env.source)
                target = as_dict(raw_build).get("target")
                image_id = _add_image(images, keys, (build[0], build[1], str(target) if target else None),
                                      slug(name if iname == "root" else iname),
                                      evidence(snap, rel, compose_key_line(snap, rel, name, "build")),
                                      snap, unresolved)
                if as_dict(raw_build).get("args"):
                    _unresolved(unresolved, f"images.{image_id}.args", "build args는 옮기지 않는다")
        if role in STORE_ROLES:
            item = {"id": name}
            if sid := _store_link(cls, datastore_ids):
                item["datastore"] = sid
            else:
                _unresolved(unresolved, f"datastores.{name}.datastore", "inventory datastores에 대응하는 범위가 없다")
            if image_id:
                item["build_image"] = image_id
            elif image:
                item["image"] = image
            ports = ports or _referenced_port(name, env.services) or ([cls["port"]] if cls.get("port") else [])
            if not ports:
                _unresolved(unresolved, f"datastores.{name}.ports", "포트를 정하는 설정이 없다")
            item.update({"ports": ports, "env": env_values, "env_names": env_names, "evidence": ev})
            stores.append(item)
            continue
        c: dict = {"id": name}
        if name in linked:
            c["workload"] = linked[name]
        if image_id:
            c["image"] = image_id
        elif image and not build:
            c["registry_image"] = image
        elif not build:
            _unresolved(unresolved, f"containers.{name}.image", "image도 build도 없다")
        if command := _command_text(svc.get("command")):
            c["command"] = command
        if entrypoint := _command_text(svc.get("entrypoint")):
            c["entrypoint"] = entrypoint
        run = compose_command(env.source, svc, artifacts)
        kind = compose_kind(name, run, cls if role == "reverse-proxy" else None) if role != "infra" else "infra"
        if not ports:
            ports = _command_port(run) or (_expose_ports(df) if kind in ENTRY_KINDS else [])
        if not ports and kind in ENTRY_KINDS:
            _unresolved(unresolved, f"containers.{name}.ports", "듣는 포트를 정하는 설정이 없다")
        restart = svc.get("restart")
        c.update({"ports": ports, "env": env_values, "env_names": env_names, "depends_on": depends_on(svc),
                  "one_shot": restart in ("no", False) or name in awaited, "evidence": ev})
        containers.append(c)
        if kind in ENTRY_KINDS and published:
            entry_cands.append((name, kind, published[0]))
    if not containers:
        return None
    for item in containers:
        missing = [d for d in item["depends_on"] if d not in kept]
        if missing:
            _unresolved(unresolved, f"containers.{item['id']}.depends_on",
                        f"배포 단위에 없는 서비스 {', '.join(missing)}(profiles·개발 도구)")
        item["depends_on"] = [d for d in item["depends_on"] if d in kept]
    entry = _pick_entry(entry_cands, unresolved, "호스트 포트를 연 web/proxy 컨테이너")
    return {"source": {"kind": "compose", "path": env.source}, "images": images, "containers": containers,
            "datastores": stores, "entry": entry, "unresolved": unresolved}


# --- k8s -----------------------------------------------------------------------

def _labels(doc: dict) -> dict:
    spec = as_dict(doc.get("spec"))
    if doc.get("kind") == "CronJob":
        spec = as_dict(as_dict(spec.get("jobTemplate")).get("spec"))
    return as_dict(as_dict(as_dict(spec.get("template")).get("metadata")).get("labels"))


def _k8s_docs(artifacts: list[ParsedArtifact], kinds: tuple[str, ...]) -> list[tuple[ParsedArtifact, dict]]:
    """종류가 kinds인 객체들(이름이 같으면 저장소 파일, 렌더 결과 순으로 먼저 본 것)."""
    seen: set[tuple] = set()
    out = []
    for art in sorted((a for a in artifacts if a.kind == "k8s"), key=lambda a: (is_build_path(a.path), a.path)):
        for doc in art.objects:
            if not isinstance(doc, dict) or doc.get("kind") not in kinds:
                continue
            name = as_dict(doc.get("metadata")).get("name")
            if isinstance(name, str) and name and (doc["kind"], name) not in seen:
                seen.add((doc["kind"], name))
                out.append((art, doc))
    return out


def _k8s_env(c: dict) -> tuple[dict[str, str | None], bool]:
    """컨테이너 env: 값이 적힌 것은 값, valueFrom(Secret·ConfigMap 참조)은 None. envFrom이 있으면 True."""
    env: dict[str, str | None] = {}
    for e in c.get("env") if isinstance(c.get("env"), list) else []:
        if isinstance(e, dict) and isinstance(e.get("name"), str):
            v = e.get("value")
            env[e["name"]] = None if v is None or "valueFrom" in e else str(v).lower() if isinstance(v, bool) else str(v)
    return env, bool(c.get("envFrom"))


def _from_k8s(snap: Snapshot, artifacts: list[ParsedArtifact], workloads: list[WorkloadInfo],
              datastore_ids: set[str]) -> dict | None:
    docs = _k8s_docs(artifacts, K8S_WORKLOAD_KINDS)
    if not docs:
        return None
    services = [doc for _, doc in _k8s_docs(artifacts, ("Service",))]
    backends = {b for _, doc in _k8s_docs(artifacts, ("Ingress",)) for b in ingress_backends(doc)}
    by_name = {w.name: w for w in workloads if w.source == "k8s"}
    unresolved: list[dict] = []
    images: list[dict] = []
    keys: dict[tuple, str] = {}
    containers: list[dict] = []
    stores: list[dict] = []
    entry_cands: list[tuple[str, str, int]] = []
    for art, doc in docs:
        name = doc["metadata"]["name"]
        pod = pod_spec(doc)
        cs = [as_dict(c) for c in pod.get("containers")] if isinstance(pod.get("containers"), list) else []
        if not cs:
            continue
        c = cs[0]
        labels = _labels(doc)
        selecting = [s for s in services if (sel := as_dict(as_dict(s.get("spec")).get("selector")))
                     and all(labels.get(k) == v for k, v in sel.items())]
        # 다른 컨테이너는 Service 이름으로 접속한다. Service가 하나면 그 이름, 없거나 여럿이면 워크로드 이름
        cid = selecting[0]["metadata"]["name"] if len(selecting) == 1 else name
        path = build_source(art.path)
        ev = evidence(snap, path, None if is_build_path(art.path) else line_of(snap, path, f"name: {name}"))
        if len(cs) > 1:
            _unresolved(unresolved, f"containers.{cid}", f"Pod의 두 번째 이후 컨테이너({len(cs) - 1}개)는 옮기지 않는다")
        image = str(c.get("image") or "")
        w = by_name.get(name)
        df = workload_dockerfile(w, artifacts) if w else dockerfile_for_image(image, artifacts)
        cls = image_class(image, df)
        role = cls["role"] if cls else None
        if role == "dev-tool":
            continue
        env, env_from = _k8s_env(c)
        env_values, env_names = _split_env(env)
        if env_from:
            _unresolved(unresolved, f"containers.{cid}.env", "envFrom(ConfigMap·Secret 전체)은 이름을 알 수 없다")
        ports = [int(p["containerPort"]) for p in c.get("ports") if isinstance(p, dict)
                 and isinstance(p.get("containerPort"), int)] if isinstance(c.get("ports"), list) else []
        if role in STORE_ROLES:
            item = {"id": cid}
            if sid := _store_link(cls, datastore_ids):
                item["datastore"] = sid
            else:
                _unresolved(unresolved, f"datastores.{cid}.datastore", "inventory datastores에 대응하는 범위가 없다")
            ports = ports or ([cls["port"]] if cls.get("port") else [])
            item.update({"image": image, "ports": ports, "env": env_values, "env_names": env_names, "evidence": ev})
            stores.append(item)
            continue
        item: dict = {"id": cid}
        if w:
            item["workload"] = w.id
        if df is not None:
            context = w.build_context if w and w.build_context is not None else recovered_context(snap, df)
            iname = image_name(image) or dockerfile_workload_name(df.path)
            item["image"] = _add_image(images, keys, (context, df.path, None), slug(cid if iname == "root" else iname),
                                       _dockerfile_evidence(snap, df), snap, unresolved)
        elif image:
            item["registry_image"] = image
        else:
            _unresolved(unresolved, f"containers.{cid}.image", "image가 없다")
        # 쿠버네티스 command는 이미지 ENTRYPOINT를, args는 CMD를 바꾼다
        if command := _command_text(c.get("args")):
            item["command"] = command
        if entrypoint := _command_text(c.get("command")):
            item["entrypoint"] = entrypoint
        kind = "reverse-proxy" if role == "reverse-proxy" else "infra" if role else (
            w.kind if w else "worker" if is_worker(name, entrypoint) else "web")
        if not ports and kind in ENTRY_KINDS:
            ports = _command_port(f"{entrypoint} {command}") or _expose_ports(df)
            if not ports:
                _unresolved(unresolved, f"containers.{cid}.ports", "듣는 포트를 정하는 설정이 없다")
        if doc["kind"] == "CronJob":
            _unresolved(unresolved, f"containers.{cid}.schedule", "CronJob 스케줄은 계약에 없다")
        item.update({"ports": ports, "env": env_values, "env_names": env_names, "depends_on": [],
                     "one_shot": doc["kind"] == "Job", "evidence": ev})
        containers.append(item)
        public = any(as_dict(s.get("spec")).get("type") in ("LoadBalancer", "NodePort") for s in selecting)
        # Service 없이 Ingress가 워크로드 이름을 가리키는 경우도 공개로 본다
        names = {s["metadata"]["name"] for s in selecting} | {cid}
        if kind in ENTRY_KINDS and ports and (public or names & backends):
            entry_cands.append((cid, kind, ports[0]))
    if not containers:
        return None
    entry = _pick_entry(entry_cands, unresolved, "Ingress·LoadBalancer·NodePort로 공개한 web/proxy 컨테이너")
    first = docs[0][0]
    return {"source": {"kind": "k8s", "path": build_source(first.path)}, "images": images,
            "containers": containers, "datastores": stores, "entry": entry, "unresolved": unresolved}


# --- 코드 ----------------------------------------------------------------------

def _image_entry(component: str) -> dict | None:
    """images.yaml 에서 이 구성 요소를 가리키는 항목(첫 번째)."""
    return next((e for e in kb.images() if component and e.get("component") == component), None)


def _is_loopback(host: str) -> bool:
    return host.startswith("127.") or host in LOOPBACK_HOSTS


def _env_defaults(snap: Snapshot, w: WorkloadInfo | None, schemes: list[str]) -> list[tuple[str, str, str, str]]:
    """워크로드 코드에서 저장소 주소(스킴)를 기본값으로 읽는 환경변수 (이름, 스킴, 호스트, 경로).
    테스트·보조 경로는 보지 않는다. 같은 이름은 먼저 찾은 기본값만."""
    if w is None or not schemes:
        return []
    root = (w.code_root or "").rstrip("/")
    found: dict[str, tuple[str, str, str]] = {}
    for rel in snap.files:
        if not rel.endswith(CODE_EXTS) or (root and not rel.startswith(root + "/")) \
                or is_test_path(rel) or is_aux_path(rel):
            continue
        for line in snap.lines(rel):
            for rx in _ENV_DEFAULT:
                for name, scheme, host, _port, path in rx.findall(line):
                    if scheme in schemes:
                        found.setdefault(name, (scheme, host, path))
    return [(name, *v) for name, v in sorted(found.items())]


def _code_stores(snap: Snapshot, containers: list[dict], apps: list[WorkloadInfo], datastores: list[dict],
                 current: list[dict], unresolved: list[dict]) -> list[dict]:
    """compose·k8s 없이 코드에서만 쓰는 저장소: 비밀번호 없이 뜨는 공식 이미지가 있으면 같은 묶음의 컨테이너로 만든다.
    앱 코드가 루프백 주소를 기본값으로 읽는 환경변수가 있으면 그 이름에 컨테이너 주소(스킴은 url_scheme 첫 값, 경로는 기본값 그대로)를
    넣는다. 하나도 넣지 못한 사용자 컨테이너는 unresolved 에 남긴다."""
    comp_of = {c["scope"]: c["component"] for c in current}
    used_ids = {c["id"] for c in containers}
    out = []
    for d in datastores:
        if d.get("status") != "confirmed":
            continue
        entry = _image_entry(comp_of.get(d["id"], ""))
        if entry is None:
            continue
        if not entry.get("image"):
            _unresolved(unresolved, f"datastores.{d['id']}", "접속 정보(비밀번호)가 필요한 저장소라 컨테이너를 만들지 않는다")
            continue
        base = entry["match"][0]
        sid, n = base, 1
        while sid in used_ids:
            n += 1
            sid = f"{base}-store" if n == 2 else f"{base}-store{n - 1}"
        used_ids.add(sid)
        port = entry.get("port")
        ev = (d.get("evidence") or [{}])[0]
        out.append({"id": sid, "datastore": d["id"], "image": entry["image"], "ports": [port] if port else [],
                    "env": {}, "env_names": [],
                    "evidence": {"path": ev.get("path", ""), "line": ev.get("line"), "snippet": ev.get("snippet", "")}})
        users = set(d.get("used_by") or [])
        for c in containers:
            if c.get("workload") not in users:
                continue
            c["depends_on"] = list(dict.fromkeys([*c["depends_on"], sid]))
            w = next((a for a in apps if a.id == c["workload"]), None)
            wired, external = False, None
            for name, _scheme, host, path in _env_defaults(snap, w, entry.get("url_scheme") or []):
                if not _is_loopback(host):
                    external = external or host
                    continue
                if not (entry.get("url_template") and port):
                    continue
                value = entry["url_template"].format(scheme=entry["url_scheme"][0], host=sid, port=port, path=path)
                if not is_secret(name, value):
                    c["env"][name] = value
                    c["env_names"] = sorted({*c["env_names"], name})
                    wired = True
            if not wired:
                _unresolved(unresolved, f"containers.{c['id']}.env",
                            f"앱이 외부 {external} 주소를 기본값으로 쓴다 (컨테이너 대신 그 주소를 환경변수로 넣어야 할 수 있다)"
                            if external else f"앱이 {sid} 주소를 읽는 환경변수를 찾지 못했다 (코드가 localhost 로 접속하면 실패)")
    return out


def _from_code(snap: Snapshot, artifacts: list[ParsedArtifact], workloads: list[WorkloadInfo],
               datastores: list[dict], current: list[dict]) -> dict | None:
    """compose·k8s가 없을 때: 코드·Dockerfile에서 찾은 앱 워크로드마다 컨테이너 하나(보통 하나뿐이다)."""
    unresolved: list[dict] = []
    apps = [w for w in workloads if is_app(w) and w.kind != "static-frontend"]
    for w in workloads:
        if w.kind == "static-frontend":
            _unresolved(unresolved, f"containers.{w.name}", "정적 프런트엔드는 컨테이너로 옮기지 않는다")
    if not apps:
        return None
    images: list[dict] = []
    keys: dict[tuple, str] = {}
    containers: list[dict] = []
    webs: list[tuple[str, str, int]] = []
    for w in apps:
        df = workload_dockerfile(w, artifacts)
        context = w.build_context if w.build_context is not None else (
            recovered_context(snap, df) if df else w.code_root)
        item: dict = {"id": w.name, "workload": w.id}
        if context is None:
            _unresolved(unresolved, f"containers.{w.name}.image", "빌드 컨텍스트를 정할 수 없다")
        else:
            item["image"] = _add_image(images, keys, (context, df.path if df else None, None), slug(w.name),
                                       _dockerfile_evidence(snap, df) if df else w.entrypoint, snap, unresolved)
        if df is None and w.command:  # Dockerfile이 없으면 만들 때 쓸 실행 명령을 넘긴다
            item["command"] = w.command
        ports = _command_port(w.command) or (_expose_ports(df) if w.kind == "web" else [])
        if not ports and w.kind == "web":
            _unresolved(unresolved, f"containers.{w.name}.ports", "듣는 포트를 정하는 설정이 없다")
        item.update({"ports": ports, "env": {}, "env_names": [], "depends_on": [],
                     "one_shot": w.kind in ONE_SHOT_KINDS, "evidence": w.entrypoint})
        containers.append(item)
        if w.kind == "web" and ports:
            webs.append((w.name, "web", ports[0]))
    entry = _pick_entry(webs, unresolved, "포트를 아는 web 컨테이너")
    stores = _code_stores(snap, containers, apps, datastores, current, unresolved)
    return {"source": {"kind": "code", "path": apps[0].entrypoint["path"]}, "images": images,
            "containers": containers, "datastores": stores, "entry": entry, "unresolved": unresolved}


def detect_deploy_units(snap: Snapshot, artifacts: list[ParsedArtifact], workloads: list[WorkloadInfo],
                        datastores: list[dict], current_components: list[dict] | None = None) -> dict:
    ids = {d["id"] for d in datastores}
    found = (_from_compose(snap, artifacts, workloads, ids) or _from_k8s(snap, artifacts, workloads, ids)
             or _from_code(snap, artifacts, workloads, datastores, current_components or []))
    if found is None:
        return {"source": None, "images": [], "containers": [], "datastores": [], "entry": None,
                "unresolved": [{"field": "containers", "why": "compose·k8s·코드 어디에서도 실행할 컨테이너를 찾지 못했다"}]}
    return found
