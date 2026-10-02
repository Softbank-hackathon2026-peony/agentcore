"""S3 범위 정하기: 앱 집계 범위(`w-*`)와 데이터 범위(인벤토리 저장소)."""

from __future__ import annotations

from dataclasses import dataclass, field

from infrafit.fit.engine import COMPUTE_FAMILIES, DATA_FAMILIES, engine_of

APP_KINDS = ("web", "worker", "scheduled", "realtime")
ENGINE_FAMILY = {"postgres": "ds", "sqlite": "ds", "redis": "ca"}  # 엔진 → 기본 계열(참고용)


@dataclass
class Scope:
    id: str
    kind: str                        # compute | data
    dims: list[dict]
    members: list[str]               # 이 범위가 대표하는 인벤토리 범위
    current: list[str] = field(default_factory=list)
    engine: str | None = None


def _family(component_id: str) -> str:
    return component_id.split(":", 1)[0]


def app_scopes(inventory: dict, profile: dict) -> list[Scope]:
    """프로필의 `w-*` 범위. 앱 집계 범위(워크로드들에서 모은 값, 예: `w-app`)가 있으면 그것만 쓴다.
    워크로드 범위의 값도 엔드포인트에서 모으면 aggregated_from을 갖기 때문에, 출처가 모두 워크로드인지로 가른다."""
    dims = [d for d in profile.get("dimensions", []) if d["scope"].startswith("w-")]
    workload_ids = {w["id"] for w in inventory.get("workloads", [])}
    aggregated = sorted({d["scope"] for d in dims if d.get("aggregated_from")
                         and set(d["aggregated_from"]) <= workload_ids and d["scope"] not in workload_ids})
    if aggregated:
        ids = aggregated
    else:
        ids = sorted({d["scope"] for d in dims})
    if not ids:
        ids = sorted(w["id"] for w in inventory.get("workloads", []) if w["kind"] in APP_KINDS)
    out = []
    for sid in ids:
        sdims = [d for d in dims if d["scope"] == sid]
        members = sorted({m for d in sdims for m in d.get("aggregated_from") or []} | {sid})
        current = sorted({c["component"] for c in inventory.get("current_components", [])
                          if c["scope"] in members and _family(c["component"]) in COMPUTE_FAMILIES})
        out.append(Scope(id=sid, kind="compute", dims=sdims, members=members, current=current))
    return out


def data_scopes(inventory: dict, profile: dict, components: dict[str, dict]) -> list[Scope]:
    """엔진(postgres|redis|sqlite)을 알 수 있는 저장소 범위."""
    out = []
    for ds in sorted(inventory.get("datastores", []), key=lambda d: d["id"]):
        current = sorted({c["component"] for c in inventory.get("current_components", [])
                          if c["scope"] == ds["id"] and _family(c["component"]) in DATA_FAMILIES})
        engines = sorted({e for e in (engine_of(c, components) for c in current) if e in ENGINE_FAMILY})
        if not engines:
            continue
        dims = [d for d in profile.get("dimensions", []) if d["scope"] == ds["id"]]
        out.append(Scope(id=ds["id"], kind="data", dims=dims, members=[ds["id"]],
                         current=current, engine=engines[0]))
    return out


def candidates_for(scope: Scope, components: dict[str, dict]) -> list[str]:
    """범위와 같은 계열의 후보(능력 표에 있는 것) + 현재 구성 요소(능력 표에 있을 때)."""
    if scope.kind == "compute":
        ids = [cid for cid, c in components.items() if c.get("family", _family(cid)) in COMPUTE_FAMILIES]
    else:
        ids = [cid for cid, c in components.items()
               if c.get("family", _family(cid)) in DATA_FAMILIES and engine_of(cid, components) == scope.engine]
    return sorted(set(ids))
