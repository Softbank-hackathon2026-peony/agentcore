"""compose 파일 이름 규칙과 서비스 정의 읽기(워크로드·이미지 서비스·환경이 함께 쓴다)."""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath

from infrafit.detect.artifacts import ParsedArtifact, as_dict
from infrafit.repo import Snapshot, parent_dir

DEVCONTAINER = ".devcontainer"  # 개발 컨테이너용 compose·Dockerfile은 배포 대상(워크로드·환경)이 아니다
# 한 디렉터리에 기본 파일이 여럿이면 docker compose와 같은 순서로 하나를 고른다
COMPOSE_BASE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
COMPOSE_PREFIXES = ("docker-compose.", "compose.", "docker-compose-")
COMPOSE_OVERRIDE_NAMES = ("compose.override.yaml", "compose.override.yml",
                          "docker-compose.override.yaml", "docker-compose.override.yml")


def compose_variant(rel: str) -> str | None:
    """compose 파일 이름의 변형 부분: 기본 파일이면 "", `docker-compose.<x>.yml`·`compose.<x>.yaml`·
    `docker-compose-<x>.yml`이면 x, 그 밖의 이름은 None."""
    name = PurePosixPath(rel).name
    if name in COMPOSE_BASE_NAMES:
        return ""
    for prefix in COMPOSE_PREFIXES:
        for suffix in (".yml", ".yaml"):
            if name.startswith(prefix) and name.endswith(suffix) and len(name) > len(prefix) + len(suffix):
                return name[len(prefix):-len(suffix)]
    return None


def is_compose_override(rel: str) -> bool:
    """`docker-compose.override.yml`·`compose.override.yaml` 등: 같은 계열 기본 파일 위에 늘 병합하는 파일(docker 동작).
    `compose.prod.override.yml`, `docker-compose-override.yml` 같은 이름은 변형 파일이다."""
    return PurePosixPath(rel).name in COMPOSE_OVERRIDE_NAMES


def compose_family(rel: str) -> str:
    """`docker-compose` 또는 `compose`: override는 같은 계열의 기본 파일에만 병합한다."""
    return "docker-compose" if PurePosixPath(rel).name.startswith("docker-compose") else "compose"


def compose_file_order(rel: str) -> tuple:
    """compose 파일을 읽는 순서: 기본 파일(docker compose가 고르는 순서), override, 변형 파일, 그 밖의 이름."""
    name = PurePosixPath(rel).name
    if name in COMPOSE_BASE_NAMES:
        return (0, COMPOSE_BASE_NAMES.index(name), rel)
    if is_compose_override(rel):
        return (1, 0, rel)
    return (2 if compose_variant(rel) else 3, 0, rel)


def _repo_path(path: str) -> str:
    p = posixpath.normpath(path)
    return "" if p == "." else p


def compose_build(compose_path: str, svc: dict) -> tuple[str, str] | None:
    """compose 서비스의 build → (빌드 컨텍스트, Dockerfile) 저장소 경로. build가 없으면 None."""
    build = svc.get("build")
    base = parent_dir(compose_path)
    if isinstance(build, str):
        context = _repo_path(posixpath.join(base, build))
        return context, _repo_path(posixpath.join(context, "Dockerfile"))
    if isinstance(build, dict):
        context = _repo_path(posixpath.join(base, str(build.get("context") or ".")))
        return context, _repo_path(posixpath.join(context, str(build.get("dockerfile") or "Dockerfile")))
    return None


def compose_services(artifacts: list[ParsedArtifact]):
    """(compose 파일, 서비스 이름, 서비스 정의). 같은 서비스가 여러 파일에 있으면 기본 파일, override, 변형 파일
    순으로 먼저 본 정의를 쓴다. 개발 컨테이너용 compose는 배포 대상이 아니므로 뺀다."""
    names: set[str] = set()
    for art in sorted((a for a in artifacts if a.kind == "compose"), key=lambda a: compose_file_order(a.path)):
        if DEVCONTAINER in PurePosixPath(art.path).parts:
            continue
        for obj in art.objects:
            if not isinstance(obj, tuple) or len(obj) != 2 or not isinstance(obj[0], str) or obj[0] in names:
                continue
            names.add(obj[0])
            yield art, obj[0], as_dict(obj[1])


def depends_on(svc: dict) -> list[str]:
    deps = svc.get("depends_on")
    if isinstance(deps, dict):
        return [str(k) for k in deps]
    return [str(d) for d in deps if isinstance(d, (str, int))] if isinstance(deps, list) else []


def service_line(snap: Snapshot, rel: str, name: str) -> int | None:
    """compose 서비스 정의 줄: 최상위 `services:` 블록 안의 `<이름>:` 키 줄 중 들여쓰기가 가장 얕은 첫 줄
    (앵커 값의 `@postgres:5432`, depends_on 맵의 같은 키, 이름이 같은 볼륨은 피한다). 없으면 None."""
    rx = re.compile(rf"^(\s+)[\"']?{re.escape(name)}[\"']?\s*:(\s|$)")
    found, inside = [], False
    for i, text in enumerate(snap.lines(rel), 1):
        if text[:1].strip() and not text.startswith("#"):  # 최상위 키에서 블록이 바뀐다
            inside = text.split(":", 1)[0].strip().strip("\"'") == "services"
        elif inside and (m := rx.match(text)):
            found.append((len(m.group(1)), i))
    return min(found)[1] if found else None
