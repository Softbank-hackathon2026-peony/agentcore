"""환경: kustomize 환경 overlay(말단, 또는 overlays 아래의 환경 디렉터리) 하나, compose 설정(기본 파일과
override, 또는 그 위의 변형 파일) 하나가 각각 환경 하나다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

from infrafit.detect.artifacts import ParsedArtifact, as_dict, build_source, dockerfile_at, is_build_path, pod_spec
from infrafit.detect.compose import (COMPOSE_BASE_NAMES, DEVCONTAINER, compose_build, compose_family,
                                     compose_variant, is_compose_override)
from infrafit.detect.images import image_name
from infrafit.detect.workloads import WorkloadInfo, compose_command, slug, workload_dockerfile
from infrafit.evidence import evidence
from infrafit.repo import Snapshot, parent_dir

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


@dataclass
class Environment:
    name: str
    source: str  # 실제 kustomization.yaml 경로, compose 환경이면 기본 파일 또는 변형 파일
    rendered: bool
    objects: list[dict] = field(default_factory=list)
    kind: str = "kustomize"  # "kustomize" | "compose" | "platform" | "ci-deploy"
    services: dict[str, dict] = field(default_factory=dict)  # compose 환경의 병합된 서비스 정의
    # compose 환경: 워크로드 id → 서비스 이름. platform·ci-deploy 환경: 배포하는 워크로드 id → ""
    members: dict[str, str] = field(default_factory=dict)
    origins: dict[str, dict[str, str]] = field(default_factory=dict)  # 서비스 → 키 → 그 값을 정한 compose 파일
    manual: bool | None = None  # ci-deploy 환경: 워크플로가 수동 실행으로만 돈다
    source_line: int | None = None  # ci-deploy 환경: 배포 step 줄

    def to_dict(self, snap: Snapshot) -> dict:
        out = {"name": self.name, "kind": self.kind, "rendered": self.rendered,
               "source": evidence(snap, self.source, self.source_line)}
        if self.kind in ("platform", "ci-deploy"):
            out["members"] = sorted(self.members)
        if self.manual is not None:
            out["manual"] = self.manual
        return out


def _leaf_dir(source: str) -> str:
    parent = str(PurePosixPath(source).parent)
    return "" if parent == "." else parent


def _env_name(leaf: str) -> str:
    """마지막 `overlays/`까지 뗀 나머지. `overlays/`가 없으면 leaf 경로 전체."""
    parts = leaf.split("/") if leaf else []
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "overlays":
            return "/".join(parts[i + 1:]) or leaf
    return leaf


def _kustomize_environments(artifacts: list[ParsedArtifact]) -> list[Environment]:
    built = sorted((a for a in artifacts if is_build_path(a.path)), key=lambda a: a.path)
    names = [_env_name(_leaf_dir(build_source(a.path))) for a in built]
    envs = []
    for art, name in zip(built, names):
        source = build_source(art.path)
        if names.count(name) > 1:  # 이름이 겹치면 leaf 경로 전체로 구분한다
            name = _leaf_dir(source)
        objects = [o for o in art.objects if isinstance(o, dict)] if art.parsed and isinstance(art.objects, list) else []
        envs.append(Environment(name or "root", source, bool(art.parsed), objects))
    return envs


# --- compose -------------------------------------------------------------------

def compose_env(value) -> dict[str, str | None] | None:
    """compose `environment`(목록 또는 맵) → 변수 이름 → 값. 둘 다 아니면 None.
    값 없는 `KEY`(목록)·`KEY:`(맵)는 실행하는 호스트의 값을 받는데 그 값은 알 수 없으므로 None(없음)으로 둔다.
    YAML 불리언은 compose가 넘기는 표기(`true`·`false`)로 둔다."""
    if isinstance(value, dict):
        return {str(k): None if v is None else str(v).lower() if isinstance(v, bool) else str(v)
                for k, v in value.items()}
    if isinstance(value, list):
        out: dict[str, str | None] = {}
        for item in value:
            k, sep, v = str(item).partition("=")
            if k:
                out[k] = v if sep else None
        return out
    return None


def _volume_target(volume) -> str:
    """볼륨 항목의 컨테이너 경로(`src:dst[:mode]`의 dst, 이름 없는 볼륨이면 그 경로, 긴 형식은 target)."""
    if isinstance(volume, dict):
        return str(volume.get("target") or volume)
    if isinstance(volume, str):
        parts = volume.split(":")
        return parts[1] if len(parts) >= 2 else parts[0]
    return repr(volume)


def _merge_value(key: str, old, new):
    """docker compose 병합: `environment`는 변수 이름 단위, `volumes`는 컨테이너 경로 단위(같으면 뒤 파일이 이긴다),
    `ports`·`expose`는 겹치지 않게 덧붙이고, 그 밖의 키와 형식이 맞지 않는 값은 뒤 파일 값으로 바꾼다."""
    if key == "environment":
        new_vars = compose_env(new)
        return new if new_vars is None else {**(compose_env(old) or {}), **new_vars}
    if not isinstance(old, list) or not isinstance(new, list):
        return new
    if key == "volumes":
        merged = list(old)
        index = {_volume_target(v): i for i, v in enumerate(merged)}
        for v in new:
            target = _volume_target(v)
            if target in index:
                merged[index[target]] = v
            else:
                index[target] = len(merged)
                merged.append(v)
        return merged
    if key in ("ports", "expose"):
        merged = list(old)
        for v in new:
            if v not in merged:
                merged.append(v)
        return merged
    return new


def _service_entries(art: ParsedArtifact) -> list[tuple[str, object]]:
    """파싱에 성공한 compose 파일의 (서비스 이름, 정의)들."""
    if not art.parsed or not isinstance(art.objects, list):
        return []
    return [obj for obj in art.objects if isinstance(obj, tuple) and len(obj) == 2 and isinstance(obj[0], str)]


def _merge(files: list[ParsedArtifact]) -> tuple[dict[str, dict], dict[str, dict[str, str]]]:
    """서비스 단위 병합(docker 규칙, _merge_value). 파싱에 실패한 파일은 건너뛴다.
    (병합된 서비스, 서비스 → 키 → 마지막으로 그 키를 쓴 파일)."""
    services: dict[str, dict] = {}
    origins: dict[str, dict[str, str]] = {}
    for art in files:
        for name, body in _service_entries(art):
            merged = services.setdefault(name, {})
            origin = origins.setdefault(name, {})
            for key, value in as_dict(body).items():
                key = str(key)
                origin[key] = art.path
                merged[key] = _merge_value(key, merged.get(key), value)
    return services, origins


def compose_environments(artifacts: list[ParsedArtifact]) -> list[Environment]:
    """디렉터리마다: 기본 파일(+같은 계열 override) 환경 하나, 변형 파일마다 기본 파일 + 변형 파일 환경 하나
    (`docker compose -f base -f variant`와 같이 override는 넣지 않는다). 변형 파일은 파싱에 성공하고 서비스가
    하나 이상 있어야 환경을 만든다. `.devcontainer/` 아래 compose는 환경이 아니다."""
    by_dir: dict[str, list[ParsedArtifact]] = {}
    for art in sorted((a for a in artifacts if a.kind == "compose"), key=lambda a: a.path):
        d = parent_dir(art.path)
        if DEVCONTAINER not in PurePosixPath(d).parts:
            by_dir.setdefault(d, []).append(art)
    envs: list[Environment] = []
    for d, files in sorted(by_dir.items()):
        bases = sorted((a for a in files if compose_variant(a.path) == ""),
                       key=lambda a: COMPOSE_BASE_NAMES.index(PurePosixPath(a.path).name))
        base = bases[:1]
        overrides = [a for a in files if is_compose_override(a.path)
                     and base and compose_family(a.path) == compose_family(base[0].path)]
        variants = [a for a in files if compose_variant(a.path) and not is_compose_override(a.path)
                    and _service_entries(a)]
        candidates = ([("compose", base[0], base + overrides)] if base else []) + [
            (f"compose.{compose_variant(v.path)}", v, base + [v]) for v in variants]
        for name, source, chain in candidates:
            services, origins = _merge(chain)
            if not services:
                continue
            envs.append(Environment(f"{name}/{d}" if d else name, source.path, True, kind="compose",
                                    services=services, origins=origins))
    # 이름이 겹치면(docker-compose.x.yml과 compose.x.yml) 경로 순 첫 번째만 둔다
    unique: dict[str, Environment] = {}
    for env in envs:
        unique.setdefault(env.name, env)
    return list(unique.values())


def _service_dockerfile(env: Environment, svc: dict) -> str | None:
    build = compose_build(env.source, svc)
    return build[1] if build else None


def match_services(env: Environment, workloads: list[WorkloadInfo], artifacts: list[ParsedArtifact]) -> None:
    """서비스 ↔ 워크로드: 이름이 같으면 대응하고, 남은 것끼리는 같은 Dockerfile이나 같은 이미지 이름으로
    서로 유일한 후보일 때만 대응한다."""
    names = set(env.services)
    for w in workloads:
        if w.name in names:
            env.members[w.id] = w.name
    taken = set(env.members.values())
    rest_services = sorted(n for n in env.services if n not in taken)
    rest = [w for w in workloads if w.id not in env.members]
    wanted = {}
    for w in rest:
        df = workload_dockerfile(w, artifacts)
        wanted[w.id] = (df.path if df else None, image_name(w.image) if w.image else "")
    pairs = set()
    for n in rest_services:
        svc = as_dict(env.services[n])
        svc_df = _service_dockerfile(env, svc)
        svc_image = image_name(str(svc.get("image") or "")) if isinstance(svc.get("image"), str) else ""
        for w in rest:
            df, image = wanted[w.id]
            if (svc_df and svc_df == df) or (svc_image and svc_image == image):
                pairs.add((n, w.id))
    for n, wid in sorted(pairs):
        if sum(1 for p in pairs if p[1] == wid) == 1 and sum(1 for p in pairs if p[0] == n) == 1:
            env.members[wid] = n


def detect_environments(artifacts: list[ParsedArtifact], workloads: list[WorkloadInfo]) -> list[Environment]:
    kustomize = _kustomize_environments(artifacts)
    taken = {e.name for e in kustomize}
    compose = compose_environments(artifacts)
    for env in compose:
        match_services(env, workloads, artifacts)
    # 대응한 워크로드가 없는 compose(데이터베이스만 띄우는 구성 등)는 환경이 아니다
    compose = [e for e in compose if e.members]
    for env in compose:
        if env.name in taken:  # kustomize 환경과 이름이 겹치면 compose 쪽에 원본 디렉터리를 붙인다
            env.name = f"{env.name}@{parent_dir(env.source) or 'root'}"
    return sorted(kustomize + compose, key=lambda e: e.name)


def env_slug(name: str) -> str:
    return slug(name.replace("/", "-"))


def is_workload_doc(doc, w: WorkloadInfo) -> bool:
    """매니페스트 객체가 워크로드 w의 객체인가: 워크로드 종류이고 이름이나 app.kubernetes.io/name 라벨이 같다."""
    if not isinstance(doc, dict) or doc.get("kind") not in WORKLOAD_KINDS:
        return False
    meta = as_dict(doc.get("metadata"))
    return meta.get("name") == w.name or as_dict(meta.get("labels")).get("app.kubernetes.io/name") == w.name


def workload_in(env: Environment, w: WorkloadInfo) -> bool:
    if env.kind == "compose":
        return w.id in env.members
    return any(is_workload_doc(doc, w) for doc in env.objects)


def env_service(env: Environment | None, w: WorkloadInfo) -> dict | None:
    """compose 환경에서 워크로드에 대응한 서비스 정의. 대응이 없거나 compose 환경이 아니면 None."""
    if env is None or env.kind != "compose" or w.id not in env.members:
        return None
    return as_dict(env.services.get(env.members[w.id]))


def env_scopes(w: WorkloadInfo, environments: list[Environment]) -> list[Environment | None]:
    """워크로드를 볼 범위(None은 환경 밖). 렌더된 환경 중 워크로드가 있는 것들.
    k8s 워크로드가 kustomize 환경에 없으면 일반 매니페스트(환경 밖)에도 있으므로 None도 둔다.
    그 밖의 워크로드는 어느 환경에도 없을 때만 None."""
    inside = [e for e in environments if e.rendered and workload_in(e, w)]
    if w.source == "k8s" and not any(e.kind == "kustomize" for e in inside):
        return [None] + inside
    return inside or [None]


def workload_container(objects: list, w: WorkloadInfo) -> dict | None:
    """매니페스트 객체들에서 워크로드 w의 컨테이너: 이미지가 w와 같은 것, 없으면 첫 번째. 객체가 없으면 None."""
    doc = next((d for d in objects if is_workload_doc(d, w)), None)
    if doc is None:
        return None
    containers = pod_spec(doc).get("containers")
    containers = [as_dict(c) for c in containers] if isinstance(containers, list) else []
    return next((c for c in containers if w.image and c.get("image") == w.image), containers[0] if containers else {})


def _rendered_command(env: Environment, w: WorkloadInfo, artifacts: list[ParsedArtifact]) -> str:
    """렌더 객체 컨테이너의 실행 명령(쿠버네티스 규칙): `command`가 있으면 command + args, args만 있으면
    이미지 최종 단계의 ENTRYPOINT + args, 둘 다 없으면 ""(워크로드 명령, 곧 이미지 CMD를 쓴다)."""
    c = workload_container(env.objects, w) or {}
    command, args = ([str(x) for x in v] if isinstance(v, list) else [] for v in (c.get("command"), c.get("args")))
    if command:
        return " ".join(command + args)
    if not args:
        return ""
    df = workload_dockerfile(w, artifacts)
    entry = str(df.get("entrypoint") or "") if df else ""
    return " ".join(([entry] if entry else []) + args)


def env_command(env: Environment | None, w: WorkloadInfo, artifacts: list[ParsedArtifact]) -> str:
    """그 환경에서 워크로드가 실행하는 명령. 환경이 정하지 않으면 워크로드의 명령."""
    cmd = ""
    if env is not None and env.kind == "compose":
        svc = env_service(env, w)
        cmd = compose_command(env.source, svc, artifacts) if svc is not None else ""
    elif env is not None:
        cmd = _rendered_command(env, w, artifacts)
    return cmd or w.command


def compose_key_line(snap: Snapshot, rel: str, service: str, key: str) -> int | None:
    """compose 파일에서 services.<service>.<key> 키가 있는 줄."""
    try:
        root = yaml.compose(snap.read(rel))
    except yaml.YAMLError:
        return None
    for path_key in ("services", service, key):
        if not isinstance(root, yaml.MappingNode):
            return None
        found = next(((k, v) for k, v in root.value if isinstance(k, yaml.ScalarNode) and k.value == path_key), None)
        if found is None:
            return None
        if path_key == key:
            return found[0].start_mark.line + 1
        root = found[1]
    return None


def env_command_evidence(snap: Snapshot, env: Environment | None, w: WorkloadInfo,
                         artifacts: list[ParsedArtifact]) -> list[dict]:
    """env_command가 어디서 왔는가: compose 서비스 `command` 줄(없으면 빌드 Dockerfile의 ENTRYPOINT·CMD 줄),
    kustomize 환경의 렌더 명령이 워크로드 명령과 다르면 그 kustomization.yaml, 그 밖에는 워크로드 진입점."""
    if env is not None and env.kind == "compose":
        svc = env_service(env, w)
        name = env.members.get(w.id)
        if svc is not None and isinstance(svc.get("command"), (str, list)) and svc.get("command"):
            rel = env.origins.get(name, {}).get("command", env.source)
            return [evidence(snap, rel, compose_key_line(snap, rel, name, "command"))]
        build = compose_build(env.source, svc) if svc is not None else None
        evs = _dockerfile_command_evidence(dockerfile_at(build[1] if build else None, artifacts))
        if evs:
            return evs
    elif env is not None:
        cmd = _rendered_command(env, w, artifacts)
        if cmd and cmd != w.command:
            return [evidence(snap, env.source)]
    # 명령이 연결된 Dockerfile의 CMD·ENTRYPOINT에서 왔으면 그 줄이 근거다
    df = workload_dockerfile(w, artifacts)
    if df is not None and env_command(env, w, artifacts) in _dockerfile_commands(df):
        return _dockerfile_command_evidence(df) or [w.entrypoint]
    return [w.entrypoint]


def _dockerfile_commands(df: ParsedArtifact) -> set[str]:
    """Dockerfile이 정하는 실행 명령 표기들: CMD만, ENTRYPOINT 뒤에 CMD."""
    full = " ".join(str(df.get(k)) for k in ("entrypoint", "cmd") if df.get(k))
    return {c for c in (str(df.get("cmd") or ""), full) if c}


def _dockerfile_command_evidence(df: ParsedArtifact | None) -> list[dict]:
    """Dockerfile 최종 이미지의 ENTRYPOINT·CMD 줄 근거(줄 순)."""
    evs = [f["evidence"] for f in (df.settings if df else [])
           if f.get("key") in ("entrypoint", "cmd") and f.get("evidence")]
    return sorted(evs, key=lambda e: e["line"] or 0)
