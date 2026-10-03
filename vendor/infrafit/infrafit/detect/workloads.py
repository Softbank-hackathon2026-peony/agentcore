"""워크로드(프로세스 단위) 찾기: k8s > compose > 코드 순서로 하나를 쓴다."""

from __future__ import annotations

import ast
import json
import posixpath
import re
import shlex
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

import yaml

from infrafit.detect.artifacts import (ParsedArtifact, as_dict, build_source, dockerfile_at, dockerfiles, is_build_path,
                                       pod_spec)
from infrafit.detect.compose import DEVCONTAINER, compose_build, compose_services, service_line
from infrafit.detect.images import base_class, image_class, image_name, service_class
from infrafit.detect.jvm import build_location, jvm_build_dirs, spring_apps, spring_web
from infrafit.detect.manifests import Manifests
from infrafit.detect.signatures import Match, is_aux_path
from infrafit.detect.testpaths import is_dev_dockerfile, is_test_dir, is_test_path
from infrafit.evidence import evidence, line_at, line_of
from infrafit.repo import Snapshot, parent_dir

WEB_FRAMEWORKS = ("next", "express", "fastify", "koa", "@nestjs/core", "hono", "fastapi", "flask", "django")
STATIC_OUTPUT_DIRS = ("dist", "build", "out")  # 정적 프런트엔드 빌드 결과 디렉터리
PROC_KINDS = {"web": "web", "worker": "worker", "clock": "scheduled", "beat": "scheduled", "scheduler": "scheduled",
              "cron": "scheduled", "release": "migration-job"}
# 표에 없는 Procfile 유형이라도 Celery beat(정기 작업 스케줄러)를 실행하면 정기 작업이다
CELERY_BEAT = re.compile(r"\bcelery\b(?:\s+(?:--[\w-]+=\S+|-[A-Za-z]\s+\S+|--[\w-]+(?:\s+[^\s-]\S*)?))*\s+beat(?=\s|$)")
# 워커 프로세스를 뜻하는 토큰 끝(`board.worker`, `jobs/worker.py` 등). `--workers 4`, `uvicorn.workers.UvicornWorker`는 아니다
WORKER_SUFFIXES = (".worker", "/worker", ":worker", "worker.py", "worker.js", "worker.ts")
# 진입점 파일이 계속 도는 프로세스라는 근거(무한 루프, 서버·소비자 루프). 없으면 실행하고 끝나는 배치·CLI(batch)
LOOP_EVIDENCE = re.compile(r"^\s*while\s+(?:True|1)\s*:|\.(?:run_forever|serve_forever|start_consuming)\(",
                           re.MULTILINE)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", text.lower()).strip("-") or "root"


@dataclass
class WorkloadInfo:
    id: str
    kind: str
    name: str
    entrypoint: dict
    status: str
    source: str
    app_dir: str = ""
    command: str = ""
    image: str = ""
    code_root: str | None = None
    build_context: str | None = None  # compose build 컨텍스트(저장소 경로)
    dockerfile: str | None = None  # compose build가 쓰는 Dockerfile(저장소 경로)
    proxy_component: str | None = None  # reverse-proxy 워크로드: 이미지 분류의 구성 요소
    root_guessed: bool = False  # 저장소에 하나뿐인 Dockerfile로 code_root를 정했다(규칙상 추측)
    framework: str = ""  # 코드에서 찾은 웹 프레임워크(Spring: spring-mvc·spring-webflux). 출력에는 쓰지 않는다
    framework_evidence: dict | None = None  # 프레임워크를 정한 의존성 줄(Spring 웹 스타터)
    schedule_evidence: dict | None = None  # 배치 진입점을 실행하는 GitHub Actions 스케줄의 cron 줄
    scaling: dict | None = None  # 확장 요구(detect/scaling.py): {min, max, autoscale, evidence, load_tests?}

    def to_dict(self) -> dict:
        out = {"id": self.id, "kind": self.kind, "name": self.name,
               "entrypoint": self.entrypoint, "status": self.status}
        if self.scaling is not None:
            out["scaling"] = self.scaling
        return out


def is_worker_command(text: str) -> bool:
    """옵션(`-`로 시작)이 아닌 토큰이 `worker`이거나 WORKER_SUFFIXES로 끝나면 워커 프로세스다."""
    for token in text.lower().split():
        token = token.strip("'\"[],")
        if token and not token.startswith("-") and (token == "worker" or token.endswith(WORKER_SUFFIXES)):
            return True
    return False


def is_worker(name: str, command: str) -> bool:
    """워커 프로세스인가: 이름 조각(`-`·`_`·`.`로 나눈)이 `worker`·`workers`이면 명령과 상관없이
    (`email-worker`가 `node dist/index.js`를 실행하는 경우 등), 아니면 명령 토큰 규칙(is_worker_command)."""
    return bool({"worker", "workers"} & set(re.split(r"[-_.]", name.lower()))) or is_worker_command(command)


def dockerfile_for_image(image: str, artifacts: list[ParsedArtifact]) -> ParsedArtifact | None:
    last = image_name(image)
    for df in dockerfiles(artifacts):
        if last and PurePosixPath(df.path).parent.name == last:
            return df
    return None


def _dockerfile_cmd_for_image(image: str, artifacts: list[ParsedArtifact]) -> str:
    df = dockerfile_for_image(image, artifacts)
    return str(df.get("cmd") or "") if df else ""


def _image_command(dockerfile: str, artifacts: list[ParsedArtifact]) -> str:
    """Dockerfile 최종 이미지의 실행 명령: ENTRYPOINT 뒤에 CMD(둘 다 최종 단계 체인에서 온다)."""
    df = dockerfile_at(dockerfile, artifacts)
    if df is None:
        return ""
    return " ".join(str(df.get(k)) for k in ("entrypoint", "cmd") if df.get(k))


def workload_dockerfile(w: WorkloadInfo, artifacts: list[ParsedArtifact]) -> ParsedArtifact | None:
    """워크로드 이미지의 Dockerfile: compose build의 Dockerfile, 이미지 이름 규칙, code_root의 Dockerfile 순서.
    코드에서 찾은 web 워크로드는 마지막으로 실행 명령을 준 Dockerfile(_cmd_dockerfile)."""
    df = dockerfile_at(w.dockerfile, artifacts)
    if df is None and w.image:
        df = dockerfile_for_image(w.image, artifacts)
    if df is None and w.code_root is not None:
        local = sorted((a for a in dockerfiles(artifacts) if parent_dir(a.path) == w.code_root),
                       key=lambda a: (PurePosixPath(a.path).name != "Dockerfile", a.path))
        df = local[0] if local else None
    if df is None and w.source == "code" and w.kind == "web":  # 실행 명령을 준 Dockerfile(저장소 루트 포함)
        df = _cmd_dockerfile(w.app_dir, artifacts)
    return df


def _code_root_for_image(image: str, artifacts: list[ParsedArtifact]) -> str | None:
    df = dockerfile_for_image(image, artifacts)
    return parent_dir(df.path) if df else None


def compose_command(compose_path: str, svc: dict, artifacts: list[ParsedArtifact]) -> str:
    """compose 서비스의 실행 명령: `command`(목록은 공백으로 잇는다), 없으면 빌드하는 이미지의 명령."""
    cmd = svc.get("command") or ""
    cmd = " ".join(str(x) for x in cmd) if isinstance(cmd, list) else cmd if isinstance(cmd, str) else ""
    build = compose_build(compose_path, svc)
    if not cmd and build:
        cmd = _image_command(build[1], artifacts)
    return cmd


def _compose_code_root(compose_path: str, svc: dict, image: str, artifacts: list[ParsedArtifact]) -> str | None:
    build = compose_build(compose_path, svc)
    if build is None:
        return _code_root_for_image(image, artifacts)
    return parent_dir(build[1])


def _cmd_dockerfile(app_dir: str, artifacts: list[ParsedArtifact]) -> ParsedArtifact | None:
    """코드 web 워크로드가 실행하는 이미지의 Dockerfile: app_dir, 없으면 저장소 루트에서 CMD가 있는 첫 Dockerfile."""
    for target in (app_dir, ""):
        for df in dockerfiles(artifacts):
            if parent_dir(df.path) == target and df.get("cmd"):
                return df
    return None


def _dockerfile_cmd_for_dir(app_dir: str, artifacts: list[ParsedArtifact]) -> str:
    df = _cmd_dockerfile(app_dir, artifacts)
    return str(df.get("cmd")) if df else ""


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def is_app(w: WorkloadInfo) -> bool:
    """앱 워크로드인가: 리버스 프록시가 아닌 것."""
    return w.kind != "reverse-proxy"


def _from_k8s(snap: Snapshot, artifacts: list[ParsedArtifact]) -> list[WorkloadInfo]:
    seen: dict[str, WorkloadInfo] = {}
    for art in artifacts:
        if art.kind != "k8s":
            continue
        for doc in art.objects:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            if kind not in ("Deployment", "StatefulSet", "Job", "CronJob"):
                continue
            name = as_dict(doc.get("metadata")).get("name")
            containers = pod_spec(doc).get("containers")
            if not isinstance(name, str) or not name or name in seen:
                continue
            if not isinstance(containers, list) or not containers or not isinstance(containers[0], dict):
                continue
            c = containers[0]
            image = str(c.get("image") or "")
            cls = image_class(image, dockerfile_for_image(image, artifacts))
            if cls and cls["role"] != "reverse-proxy":  # 저장소·기반 서비스 이미지는 워크로드가 아니다
                continue
            cmd = " ".join(str(x) for x in _as_list(c.get("command")) + _as_list(c.get("args")))
            text = f"{name} {cmd}".lower()
            if cls:
                wkind = "reverse-proxy"
            elif kind == "CronJob":
                wkind = "scheduled"
            elif kind == "Job":
                wkind = "migration-job" if "migrat" in text else "worker"
            else:
                wkind = "worker" if is_worker(name, cmd) else "web"
            # 렌더된 kustomize 결과(`...#build`)는 실제 파일이 아니므로 kustomization.yaml을 근거로 쓴다
            path = build_source(art.path)
            line = None if is_build_path(art.path) else line_of(snap, path, f"name: {name}")
            seen[name] = WorkloadInfo(
                id=f"w-{slug(name)}", kind=wkind, name=name, entrypoint=evidence(snap, path, line),
                status="confirmed", source="k8s", image=image,
                command=cmd or _dockerfile_cmd_for_image(image, artifacts),
                code_root=_code_root_for_image(image, artifacts), proxy_component=cls["component"] if cls else None)
    return list(seen.values())


def compose_kind(name: str, cmd: str, cls: dict | None) -> str:
    """compose 앱 서비스의 워크로드 종류: 리버스 프록시 이미지면 reverse-proxy, 이름·명령에 `migrat`가 있으면
    migration-job, 워커 규칙(is_worker)에 맞으면 worker, 아니면 web."""
    if cls:
        return "reverse-proxy"
    return "migration-job" if "migrat" in f"{name} {cmd}".lower() else "worker" if is_worker(name, cmd) else "web"


def _from_compose(snap: Snapshot, artifacts: list[ParsedArtifact]) -> list[WorkloadInfo]:
    out: list[WorkloadInfo] = []
    for art, name, svc in compose_services(artifacts):
        image = str(svc.get("image") or "")
        cls = service_class(art, svc, artifacts)
        if cls and cls["role"] != "reverse-proxy":  # 저장소·기반 서비스·개발 도구 이미지는 워크로드가 아니다
            continue
        build = compose_build(art.path, svc)
        cmd = compose_command(art.path, svc, artifacts)
        wkind = compose_kind(name, cmd, cls)
        out.append(WorkloadInfo(
            id=f"w-{slug(name)}", kind=wkind, name=name,
            entrypoint=evidence(snap, art.path, service_line(snap, art.path, name)),
            status="confirmed", source="compose", image=image, command=cmd,
            code_root=_compose_code_root(art.path, svc, image, artifacts),
            build_context=build[0] if build else None, dockerfile=build[1] if build else None,
            proxy_component=cls["component"] if cls else None))
    return out


def _from_code(snap: Snapshot, manifests: Manifests, artifacts: list[ParsedArtifact]) -> list[WorkloadInfo]:
    """매니페스트·빌드 파일·Procfile이 있는 디렉터리마다 워크로드. 테스트 경로(testpaths.is_test_dir) 아래의 것은
    배포 대상이 아니므로 뺀다."""
    found: dict[tuple[str, str], dict] = {}
    for d, deps in sorted(manifests.deps_by_dir.items()):
        if is_test_dir(d):
            continue
        web_fw = next((fw for fw in WEB_FRAMEWORKS if fw in deps), None)
        if web_fw:
            found[("web", d)] = {"dep": web_fw}
        elif "vite" in deps:
            found[("static-frontend", d)] = {"dep": "vite"}
    for key, info in spring_apps(snap, manifests).items():
        if not is_test_dir(key[1]):
            found.setdefault(key, info)
    for key, (cmd, rel, line) in sorted(manifests.procfile.items()):
        d, proc = key.split(":", 1)
        wkind = PROC_KINDS.get(proc) or ("scheduled" if CELERY_BEAT.search(cmd) else None)
        if not wkind or is_test_dir(d):
            continue
        entry = found.setdefault((wkind, d), {})
        entry.setdefault("proc", (cmd, rel, line))

    by_kind: dict[str, list[str]] = {}
    for wkind, d in found:
        by_kind.setdefault(wkind, []).append(d)

    out: list[WorkloadInfo] = []
    ids: set[str] = set()
    for (wkind, d), info in sorted(found.items()):
        short = "static" if wkind == "static-frontend" else wkind
        wid = ""
        if "spring" in info:
            entry, framework, name = info["spring"]
            wid = f"w-{slug(name)}"
            if wid in ids:
                wid, name = f"w-{slug(name)}-{slug(d)}", f"{name}-{slug(d)}"
            ids.add(wid)
            w = WorkloadInfo(id=wid, kind=wkind, name=name, entrypoint=entry,
                             status="confirmed" if framework else "candidate", source="code", app_dir=d,
                             code_root=d, framework=framework, framework_evidence=entry if framework else None)
            # 실행 명령은 연결된 Dockerfile의 마지막 체인 ENTRYPOINT+CMD, Procfile이 있으면 그 명령
            df = workload_dockerfile(w, artifacts)
            w.command = (_image_command(df.path, artifacts) if df else "") or (info["proc"][0] if "proc" in info else "")
            out.append(w)
            continue
        # 이름: 그 종류가 하나뿐이면 종류 이름(web·worker·static 등), 여럿이면 디렉터리 이름(겹치면 디렉터리 경로)
        names = [short] if len(by_kind[wkind]) == 1 or not d else [PurePosixPath(d).name]
        names += [slug(d), f"{short}-{slug(d)}"] if d else [f"{short}-root"]
        name = next((slug(n) for n in names if f"w-{slug(n)}" not in ids), slug(f"{short}-{d}-{len(ids)}"))
        wid = f"w-{name}"
        ids.add(wid)
        if "dep" in info:
            dep = info["dep"]
            rel, line = manifests.deps[dep]
            # 같은 의존성이 여러 디렉터리에 있으면 이 디렉터리의 매니페스트 줄을 근거로 쓴다
            for pattern in ("package.json", "requirements.txt", "pyproject.toml"):
                local = f"{d}/{pattern}" if d else pattern
                if snap.exists(local):
                    ln = line_of(snap, local, f'"{dep}"' if pattern == "package.json" else dep)
                    if ln:
                        rel, line = local, ln
                        break
            entry = evidence(snap, rel, line)
        else:
            _, rel, line = info["proc"]
            entry = evidence(snap, rel, line)
        proc_cmd = info["proc"][0] if "proc" in info else ""
        start = manifests.scripts.get(f"{d}:start")
        start_cmd = start[0] if start else ""
        if wkind == "web":
            # 이미지가 있으면 그 CMD가 실제로 배포되는 실행 명령이다
            command = _dockerfile_cmd_for_dir(d, artifacts) or proc_cmd or start_cmd
        else:
            command = proc_cmd
        # 웹 프레임워크로 찾은 워크로드는 실행 명령이 워커 명령일 때만 워커다(`worker/` 이름만으로는 아니다:
        # HTTP로 일을 받는 워커 서비스가 있다)
        if wkind == "web" and is_worker_command(command):
            wkind = "worker"
        out.append(WorkloadInfo(id=wid, kind=wkind, name=name, entrypoint=entry,
                                status="confirmed", source="code", app_dir=d, command=command, code_root=d))
    return out


def _app_dockerfiles(workloads: list[WorkloadInfo], artifacts: list[ParsedArtifact]) -> list[ParsedArtifact]:
    """어떤 워크로드에도 연결되지 않은 앱 Dockerfile: 마지막 체인에 CMD나 ENTRYPOINT가 있고, 마지막 FROM이
    리버스 프록시 이미지가 아니며, 개발 컨테이너용·개발·테스트용이 아닌 것. 경로 순."""
    linked = {df.path for w in workloads if (df := workload_dockerfile(w, artifacts))}
    return [df for df in dockerfiles(artifacts)
            if df.path not in linked and DEVCONTAINER not in PurePosixPath(df.path).parts
            and not is_dev_dockerfile(df.path)
            and (df.get("cmd") or df.get("entrypoint"))
            and (base_class(df) or {}).get("role") != "reverse-proxy"]


def dockerfile_workload_name(path: str) -> str:
    """`<x>.Dockerfile`·`Dockerfile.<x>`이면 x, 아니면 Dockerfile 디렉터리 이름(저장소 루트는 root)."""
    name = PurePosixPath(path).name
    if name.endswith(".Dockerfile") and len(name) > len(".Dockerfile"):
        return name[:-len(".Dockerfile")]
    if name.startswith("Dockerfile.") and len(name) > len("Dockerfile."):
        return name[len("Dockerfile."):]
    return PurePosixPath(path).parent.name or "root"


def _copy_sources(df: ParsedArtifact) -> list[str]:
    """모든 단계의 COPY·ADD 원본 경로들(`--from`으로 다른 단계·이미지에서 가져오는 것, URL, heredoc은 뺀다)."""
    out: list[str] = []
    for op, arg, _ in (x for x in df.objects if isinstance(x, tuple) and len(x) == 3):
        if op not in ("COPY", "ADD") or not isinstance(arg, str):
            continue
        try:
            parts = json.loads(arg) if arg.startswith("[") else shlex.split(arg)
        except (ValueError, json.JSONDecodeError):
            parts = arg.split()
        parts = [str(p) for p in parts]
        if any(p.startswith("--from") for p in parts):
            continue
        args = [p for p in parts if not p.startswith("--")]
        out += [p for p in args[:-1] if "://" not in p and not p.startswith("<<")]
    return out


def _repo_has(snap: Snapshot, path: str) -> bool:
    """저장소에 path(파일·디렉터리·glob)가 있는가."""
    p = posixpath.normpath(path)
    if p in ("", "."):
        return True
    if p.startswith(".."):
        return False
    if any(ch in p for ch in "*?["):
        return any(fnmatchcase(f, p) or fnmatchcase(f, p + "/*") for f in snap.files)
    return snap.exists(p) or any(f.startswith(p + "/") for f in snap.files)


def recovered_context(snap: Snapshot, df: ParsedArtifact) -> str:
    """Dockerfile만으로 찾은 워크로드의 빌드 컨텍스트: COPY·ADD 원본이 모두 저장소 루트 기준으로는 있고
    Dockerfile 디렉터리 기준으로는 없으면 저장소 루트(""), 아니면 Dockerfile 디렉터리."""
    d = parent_dir(df.path)
    sources = _copy_sources(df)
    if d and sources and all(_repo_has(snap, src) and not _repo_has(snap, posixpath.join(d, src))
                             for src in sources):
        return ""
    return d


def _from_dockerfiles(snap: Snapshot, workloads: list[WorkloadInfo],
                      artifacts: list[ParsedArtifact]) -> list[WorkloadInfo]:
    """앱 워크로드를 못 찾았을 때: 연결되지 않은 앱 Dockerfile마다 후보 워크로드 하나.
    code_root는 빌드 컨텍스트 후보 규칙(1b F3)처럼 Dockerfile 디렉터리로 두되, COPY·ADD 원본으로 보아 저장소
    루트가 컨텍스트이면 루트로 둔다(recovered_context)."""
    ids = {w.id for w in workloads}
    out: list[WorkloadInfo] = []
    for df in _app_dockerfiles(workloads, artifacts):
        name = dockerfile_workload_name(df.path)
        command = _image_command(df.path, artifacts)
        wid = f"w-{slug(name)}"
        if wid in ids:
            wid = f"w-{slug(df.path)}"
        if wid in ids:
            continue
        ids.add(wid)
        fact = next((f for key in ("cmd", "entrypoint") for f in df.settings if f["key"] == key), None)
        entry = (fact or {}).get("evidence") or evidence(snap, df.path)
        d = parent_dir(df.path)
        context = recovered_context(snap, df)
        out.append(WorkloadInfo(id=wid, kind="worker" if is_worker(name, command) else "web", name=name,
                                entrypoint=entry, status="candidate", source="dockerfile", app_dir=d,
                                command=command, code_root=context, dockerfile=df.path,
                                build_context=None if context == d else context))
    return out


def _link_single_dockerfile(workloads: list[WorkloadInfo], artifacts: list[ParsedArtifact]) -> None:
    """이미지만 있는 k8s·compose 앱 워크로드에 Dockerfile이 없고, 연결되지 않은 앱 Dockerfile이 정확히 하나면
    그것을 연결한다(code_root, 비어 있으면 실행 명령). 연결이 필요한 워크로드가 여럿이면 모두 같은 이미지 이름일
    때만 연결한다(같은 이미지를 명령만 바꿔 쓰는 경우). 이렇게 정한 code_root는 추측이다."""
    apps = _app_dockerfiles(workloads, artifacts)
    if len(apps) != 1:
        return
    df = apps[0]
    needing = [w for w in workloads if w.source in ("k8s", "compose") and is_app(w) and w.dockerfile is None
               and workload_dockerfile(w, artifacts) is None]
    if len({image_name(w.image) for w in needing}) > 1:
        return
    for w in needing:
        w.dockerfile, w.code_root, w.root_guessed = df.path, parent_dir(df.path), True
        w.command = w.command or _image_command(df.path, artifacts)


def _spring_framework_at_root(snap: Snapshot, manifests: Manifests, workloads: list[WorkloadInfo]) -> None:
    """k8s·compose 앱 워크로드: code_root 모듈의 빌드 파일에 Spring 웹 스타터가 있으면 그 프레임워크와 의존성 줄."""
    build_dirs = set(jvm_build_dirs(snap))
    for w in workloads:
        if w.source not in ("k8s", "compose") or not is_app(w) or w.framework or w.code_root not in build_dirs:
            continue
        web = spring_web(manifests.deps_by_dir.get(w.code_root, set()))
        loc = build_location(manifests, web[0], w.code_root) if web else None
        if loc:
            w.framework, w.framework_evidence = web[1], evidence(snap, *loc)


def _bundled_static(workloads: list[WorkloadInfo], artifacts: list[ParsedArtifact]) -> set[str]:
    """빌드 결과 디렉터리(dist·build·out)가 다른 워크로드의 Dockerfile에 COPY되는 정적 프런트엔드 id들:
    그 이미지의 일부이지 따로 배포되는 워크로드가 아니다. COPY 원본은 그 워크로드의 빌드 컨텍스트,
    Dockerfile 디렉터리, 저장소 루트 기준으로 본다."""
    copied: set[str] = set()
    for w in workloads:
        df = workload_dockerfile(w, artifacts) if w.kind != "static-frontend" else None
        if df is None:
            continue
        contexts = {c for c in (w.build_context, parent_dir(df.path), "") if c is not None}
        copied |= {posixpath.normpath(posixpath.join(c, src)) for c in contexts for src in _copy_sources(df)}
    out: set[str] = set()
    for w in workloads:
        if w.kind != "static-frontend":
            continue
        outputs = [posixpath.join(w.app_dir, o) if w.app_dir else o for o in STATIC_OUTPUT_DIRS]
        if any(p == o or p.startswith(o + "/") for p in copied for o in outputs):
            out.add(w.id)
    return out



# --- 배치·CLI 진입점(워크로드가 하나도 없을 때) ---------------------------------

GHA_SCHEDULE = "sc:github/actions-schedule/default"
_PY_MAIN = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", re.MULTILINE)
_RUN_SPLIT = re.compile(r"[\s;&|()]+")


@dataclass(frozen=True)
class _Entry:
    path: str  # 근거 파일(파이썬 파일 또는 매니페스트)
    line: int | None
    name: str  # 워크로드 이름: 파일·스크립트 이름
    file: str | None = None  # 파이썬 진입점 파일(실행 명령에서 경로로 찾는다)
    script: str | None = None  # 스크립트 이름(실행 명령에서 낱말로 찾는다)


def _py_entries(snap: Snapshot) -> list[_Entry]:
    out: list[_Entry] = []
    for rel in snap.glob("**/*.py"):
        p = PurePosixPath(rel)
        if p.name == "setup.py":
            continue
        text = snap.read(rel)
        m = _PY_MAIN.search(text)
        if p.name == "__main__.py":
            name = p.parent.name or "main"
        elif m:
            name = p.stem
        else:
            continue
        out.append(_Entry(rel, line_at(text, m.start()) if m else 1, name, file=rel))
    return out


def _toml_key_line(snap: Snapshot, rel: str, key: str) -> int | None:
    rx = re.compile(rf"""^\s*["']?{re.escape(key)}["']?\s*=""")
    return next((i for i, text in enumerate(snap.lines(rel), 1) if rx.match(text)), None)


def _script_entries(snap: Snapshot) -> list[_Entry]:
    """package.json `bin`, pyproject `[project.scripts]`(·`[tool.poetry.scripts]`)의 스크립트."""
    out: list[_Entry] = []
    for rel in snap.glob("**/package.json"):
        try:
            data = json.loads(snap.read(rel))
        except (json.JSONDecodeError, ValueError):
            continue
        bin_ = data.get("bin") if isinstance(data, dict) else None
        if isinstance(bin_, str) and bin_ and isinstance(data.get("name"), str) and data["name"]:
            name = data["name"].rsplit("/", 1)[-1]
            out.append(_Entry(rel, line_of(snap, rel, '"bin"'), name, script=name))
        elif isinstance(bin_, dict):
            for name in sorted(k for k in bin_ if isinstance(k, str) and k):
                out.append(_Entry(rel, line_of(snap, rel, f'"{name}"'), name, script=name))
    for rel in snap.glob("**/pyproject.toml"):
        try:
            data = tomllib.loads(snap.read(rel))
        except (tomllib.TOMLDecodeError, ValueError):
            continue
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        poetry = as_dict(as_dict(data.get("tool")).get("poetry"))
        for table in (project.get("scripts"), poetry.get("scripts")):
            for name in sorted(k for k in as_dict(table) if isinstance(k, str) and k):
                out.append(_Entry(rel, _toml_key_line(snap, rel, name), name, script=name))
    return out


def _runs_entry(run: str, workdir: str, e: _Entry) -> bool:
    """워크플로 step의 run 명령이 진입점을 실행하는가: 파일 경로(작업 디렉터리 기준, 또는 경로 끝이 같은 것),
    `python -m 모듈`, 스크립트 이름 낱말."""
    tokens = [t.strip("'\"") for t in _RUN_SPLIT.split(run) if t.strip("'\"")]
    for i, tok in enumerate(tokens):
        if e.script is not None and tok == e.script:
            return True
        if e.file is None:
            continue
        if i > 0 and tokens[i - 1] == "-m":  # python -m a.b → a/b.py 또는 a/b/__main__.py
            mod = tok.replace(".", "/")
            if e.file in (f"{mod}.py", f"{mod}/__main__.py") or e.file.endswith((f"/{mod}.py", f"/{mod}/__main__.py")):
                return True
        if tok.endswith(".py"):
            for cand in {posixpath.normpath(tok), posixpath.normpath(posixpath.join(workdir, tok))}:
                if e.file == cand or e.file.endswith("/" + cand.lstrip("./")):
                    return True
    return False


def _scheduled_runs(snap: Snapshot) -> list[tuple[str, int | None, str, str]]:
    """`on: schedule` GitHub Actions 워크플로의 (파일, cron 줄, run 명령, 작업 디렉터리)들. 읽을 수 없는 파일은 건너뛴다."""
    out = []
    for rel in sorted(snap.glob(".github/workflows/*.yml") + snap.glob(".github/workflows/*.yaml")):
        try:
            data = yaml.safe_load(snap.read(rel))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        on = data.get("on", data.get(True))  # YAML 1.1에서 `on` 키는 true로 읽힌다
        if not (isinstance(on, dict) and "schedule" in on):
            continue
        cron = line_of(snap, rel, "cron")
        for job in as_dict(data.get("jobs")).values():
            job = as_dict(job)
            job_dir = as_dict(as_dict(as_dict(job.get("defaults")).get("run"))).get("working-directory")
            for step in _as_list(job.get("steps")):
                step = as_dict(step)
                run = step.get("run")
                wd = step.get("working-directory", job_dir)
                if isinstance(run, str):
                    out.append((rel, cron, run, wd if isinstance(wd, str) else ""))
    return out


def _stdlib_http_server(text: str) -> bool:
    """실제 표준 HTTP 서버 생성자 호출은 serve_forever 워커 오탐에서 제외한다."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    constructors = {alias.asname or alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module == "http.server"
                    for alias in node.names if alias.name in {"HTTPServer", "ThreadingHTTPServer"}}
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id in constructors for node in ast.walk(tree))


def _from_entrypoints(snap: Snapshot) -> list[WorkloadInfo]:
    """워크로드가 하나도 없을 때: 테스트·보조 디렉터리가 아닌 곳의 진입점(파이썬 `__main__` 블록·`__main__.py`,
    package.json `bin`, pyproject 스크립트)을 디렉터리마다 하나(경로 순 첫 번째) 후보 워크로드로 만든다.
    `on: schedule` 워크플로가 실행하면 scheduled(근거: cron 줄), 진입점 파이썬 파일에 루프 근거(LOOP_EVIDENCE)가
    있으면 worker, 아니면 실행하고 끝나는 batch."""
    entries = [e for e in _py_entries(snap) + _script_entries(snap)
               if not is_test_path(e.path) and not is_aux_path(e.path)]
    first: dict[str, _Entry] = {}
    for e in sorted(entries, key=lambda e: (e.path, e.line or 0, e.name)):
        first.setdefault(parent_dir(e.path), e)
    runs = _scheduled_runs(snap)
    out: list[WorkloadInfo] = []
    ids: set[str] = set()
    for d, e in sorted(first.items()):
        wid = f"w-{slug(e.name)}"
        if wid in ids:
            wid = f"w-{slug(e.name)}-{slug(d)}"
        if wid in ids:
            continue
        ids.add(wid)
        sched = next(((rel, cron) for rel, cron, run, wd in runs if _runs_entry(run, wd, e)), None)
        text = snap.read(e.file) if e.file else ""
        loops = bool(LOOP_EVIDENCE.search(text))
        http_server = _stdlib_http_server(text)
        kind = "scheduled" if sched else "web" if http_server else "worker" if loops else "batch"
        out.append(WorkloadInfo(
            id=wid, kind=kind, name=wid[2:], entrypoint=evidence(snap, e.path, e.line),
            status="candidate", source="code", app_dir=d, code_root=d,
            schedule_evidence=evidence(snap, sched[0], sched[1], "tech") if sched else None))
    return out


def schedule_matches(workloads: list[WorkloadInfo]) -> list[Match]:
    """배치 진입점을 실행하는 GitHub Actions 스케줄: 스케줄러 구성 요소(근거: cron 줄, 같은 것은 하나만)."""
    evs: list[dict] = []
    for w in workloads:
        if w.schedule_evidence and w.schedule_evidence not in evs:
            evs.append(w.schedule_evidence)
    return [Match("gha-schedule", GHA_SCHEDULE, "scheduler", "confirmed", tuple(evs))] if evs else []


def detect_workloads(snap: Snapshot, manifests: Manifests, artifacts: list[ParsedArtifact]) -> list[WorkloadInfo]:
    workloads = _from_k8s(snap, artifacts) or _from_compose(snap, artifacts)
    if not any(is_app(w) for w in workloads):
        # k8s·compose에 앱이 없으면(프록시만 있는 경우 포함) 코드 탐지도 합친다
        ids = {w.id for w in workloads}
        for w in _from_code(snap, manifests, artifacts):
            if w.id in ids:
                w.id = f"{w.id}-{slug(w.app_dir)}"
                w.name = w.id[2:]
            if w.id not in ids:
                ids.add(w.id)
                workloads.append(w)
    if not any(is_app(w) for w in workloads):
        workloads += _from_dockerfiles(snap, workloads, artifacts)
    if not workloads:
        workloads = _from_entrypoints(snap)
    _link_single_dockerfile(workloads, artifacts)
    bundled = _bundled_static(workloads, artifacts)
    workloads = [w for w in workloads if w.id not in bundled]
    _spring_framework_at_root(snap, manifests, workloads)
    return sorted(workloads, key=lambda w: w.id)
