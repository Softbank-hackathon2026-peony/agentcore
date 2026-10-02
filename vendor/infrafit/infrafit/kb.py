"""knowledge/ 아래 YAML 지식 베이스 로더."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

import yaml

KB_DIR = Path(__file__).resolve().parent.parent / "knowledge"


def _load(rel: str) -> dict:
    return yaml.safe_load((KB_DIR / rel).read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def catalog() -> dict[str, dict]:
    return {c["id"]: c for c in _load("components/catalog.yaml")["components"]}


@lru_cache(maxsize=1)
def signatures() -> tuple[dict, ...]:
    out: list[dict] = []
    for path in sorted((KB_DIR / "signatures").glob("*.yaml")):
        if path.name in ("watchlist.yaml", "external.yaml"):
            continue
        out.extend(yaml.safe_load(path.read_text(encoding="utf-8"))["signatures"])
    return tuple(out)


@lru_cache(maxsize=1)
def watchlist() -> tuple[str, ...]:
    return tuple(_load("signatures/watchlist.yaml")["packages"])


@lru_cache(maxsize=1)
def external() -> tuple[dict, ...]:
    """외부 서비스 시그니처(knowledge/signatures/external.yaml)."""
    return tuple(_load("signatures/external.yaml")["services"])


@lru_cache(maxsize=1)
def deploy() -> tuple[dict, ...]:
    """CI 배포 명령 패턴(knowledge/deploy.yaml)."""
    return tuple(_load("deploy.yaml")["deploy"])


@lru_cache(maxsize=1)
def defaults() -> tuple[dict, ...]:
    return tuple(_load("defaults.yaml")["defaults"])


@lru_cache(maxsize=1)
def images() -> tuple[dict, ...]:
    return tuple(_load("images.yaml")["images"])


@lru_cache(maxsize=1)
def implicit_routes() -> tuple[dict, ...]:
    return tuple(_load("implicit_routes.yaml").get("implicit_routes") or ())


@lru_cache(maxsize=1)
def unmapped_signatures() -> tuple[dict, ...]:
    """구성 요소 ID가 아직 없어 unmapped label로 내는 시그니처(signatures/*.yaml의 `unmapped:` 목록)."""
    out: list[dict] = []
    for path in sorted((KB_DIR / "signatures").glob("*.yaml")):
        if path.name == "watchlist.yaml":
            continue
        out.extend(yaml.safe_load(path.read_text(encoding="utf-8")).get("unmapped") or [])
    return tuple(out)


@lru_cache(maxsize=1)
def kb_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(KB_DIR.rglob("*.yaml")):
        digest.update(path.relative_to(KB_DIR).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def iter_conditions(cond: dict) -> Iterator[dict]:
    if "any" in cond or "all" in cond:
        for child in cond.get("any", []) + cond.get("all", []):
            yield from iter_conditions(child)
    else:
        yield cond
