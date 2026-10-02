"""컨테이너 이미지 분류(knowledge/images.yaml)와 워크로드가 아닌 compose 이미지 서비스."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from infrafit import kb
from infrafit.detect.artifacts import ParsedArtifact, dockerfile_at
from infrafit.detect.compose import compose_build, compose_services, depends_on, service_line
from infrafit.evidence import evidence
from infrafit.repo import Snapshot


def image_keys(image: str) -> list[str]:
    """분류에 쓰는 이름들: 레지스트리·태그·다이제스트를 뗀 마지막 이름, 그리고 `저장소/이름`."""
    parts = image.strip().lower().split("@")[0].split("/")
    parts[-1] = parts[-1].split(":")[0]
    if not parts[-1]:
        return []
    return [parts[-1]] + (["/".join(parts[-2:])] if len(parts) >= 2 else [])


def image_name(image: str) -> str:
    """레지스트리·태그·다이제스트를 뗀 이미지의 마지막 이름."""
    keys = image_keys(image)
    return keys[0] if keys else ""


def classify_image(image) -> dict | None:
    """knowledge/images.yaml로 이미지를 분류한다: {role, component, hosting_hint, label}, 맞는 항목이 없으면 None.
    label은 패턴에 맞은 이름(마지막 이름 또는 `저장소/이름`)이다."""
    keys = image_keys(image) if isinstance(image, str) else []
    for entry in kb.images():
        for pattern in entry.get("match") or []:
            key = next((k for k in keys if fnmatchcase(k, str(pattern).lower())), None)
            if key:
                return {"role": entry.get("role"), "component": entry.get("component"),
                        "hosting_hint": entry.get("hosting_hint"), "label": key}
    return None


def base_class(df: ParsedArtifact | None) -> dict | None:
    """Dockerfile 마지막 FROM 이미지의 분류."""
    return classify_image(str(df.get("base_image") or "")) if df else None


def image_class(image: str, df: ParsedArtifact | None) -> dict | None:
    """워크로드 이미지의 분류: 이미지 이름, 없으면 빌드 Dockerfile의 마지막 FROM."""
    return classify_image(image) or base_class(df)


def service_class(art: ParsedArtifact, svc: dict, artifacts: list[ParsedArtifact]) -> dict | None:
    """compose 서비스의 분류: image, 없으면 build Dockerfile의 마지막 FROM."""
    build = compose_build(art.path, svc)
    return image_class(str(svc.get("image") or ""), dockerfile_at(build[1] if build else None, artifacts))


@dataclass
class ImageService:
    """이미지 분류로 워크로드가 아닌 compose 서비스(저장소·캐시·큐·기반 서비스)."""
    name: str
    label: str
    role: str
    component: str | None
    hosting_hint: str | None
    evidence: dict
    dependents: list[str] = field(default_factory=list)  # 이 서비스를 depends_on하는 서비스 이름


def image_services(snap: Snapshot, artifacts: list[ParsedArtifact]) -> list[ImageService]:
    """워크로드가 아닌 분류된 compose 서비스들(reverse-proxy·dev-tool 제외), 이름 순."""
    services = list(compose_services(artifacts))
    out: list[ImageService] = []
    for art, name, svc in services:
        cls = service_class(art, svc, artifacts)
        if not cls or cls["role"] in ("reverse-proxy", "dev-tool"):
            continue
        out.append(ImageService(
            name=name, label=cls["label"], role=cls["role"], component=cls["component"],
            hosting_hint=cls["hosting_hint"], evidence=evidence(snap, art.path, service_line(snap, art.path, name)),
            dependents=sorted(n for _, n, other in services if name in depends_on(other))))
    return sorted(out, key=lambda s: s.name)
