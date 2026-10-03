"""S3 범위 정하기: 워크로드마다 compute 범위와 데이터 범위(인벤토리 저장소)."""

from __future__ import annotations

from dataclasses import dataclass, field

from infrafit.fit.engine import COMPUTE_FAMILIES, DATA_FAMILIES, engine_of

APP_KINDS = ("web", "worker", "scheduled", "realtime", "batch")
# 호스팅이 필요한 워크로드 종류: 앱 워크로드 + 리버스 프록시(nginx 등도 어딘가에서 돌아야 한다)
HOSTED_KINDS = APP_KINDS + ("reverse-proxy",)
# 리버스 프록시 범위의 A1 값(knowledge/profile_detectors.yaml A1 어휘)
PROXY_A1 = "리버스 프록시"
# 엔진만 알려진(호스팅 미확인·로컬) 저장소의 공급자. 이 밖이면서 능력 표의 클라우드도 아닌 공급자(supabase, firebase 등)는
# BaaS로 보고 같은 엔진의 관리형으로 바꾸지 않는다.
PLAIN_VENDORS = ("unspecified", "local")
ENGINE_FAMILY = {"postgres": "ds", "sqlite": "ds", "redis": "ca"}  # 엔진 → 기본 계열(참고용)


@dataclass
class Scope:
    id: str
    kind: str                        # compute | data
    dims: list[dict]
    members: list[str]               # 이 범위가 대표하는 인벤토리 범위
    current: list[str] = field(default_factory=list)
    engine: str | None = None
    external: bool = False           # BaaS 등 현재 구성 요소를 그대로 두는 범위
    workload_kind: str | None = None  # compute 범위: 인벤토리 워크로드 종류
    min_replicas: int = 1             # compute 범위: 저장소가 밝힌 최소 레플리카(scaling.min, 없으면 1)


def _family(component_id: str) -> str:
    return component_id.split(":", 1)[0]


def app_scopes(inventory: dict, profile: dict) -> list[Scope]:
    """호스팅이 필요한 워크로드(HOSTED_KINDS)마다 compute 범위 하나.

    차원 값은 그 워크로드 자신의 프로필 행이다. 앱 집계 범위(`w-app`: 워크로드가 아닌 `w-*` 범위)의 값은
    어느 워크로드에도 행이 없는 차원(가정만 있는 D1·G3 등)일 때만 빌려 온다. 그래서 B1·D6처럼 워크로드마다
    정하는 차원은 다른 워크로드의 값이 섞이지 않는다. `w-app`은 설명용이고 판정 범위가 아니다.
    리버스 프록시는 앱 워크로드가 있을 때만 범위가 된다(프로필 행이 없어 빌려 온 값과, 종류에서 온 A1 "리버스 프록시"를
    갖는다)."""
    workloads = sorted((w for w in inventory.get("workloads", []) if w["kind"] in HOSTED_KINDS),
                       key=lambda w: w["id"])
    # 앱 워크로드 없이 리버스 프록시만 있으면(정적 파일 서빙 등) compute를 고르지 않는다
    if not any(w["kind"] in APP_KINDS for w in workloads):
        return []
    workload_ids = {w["id"] for w in inventory.get("workloads", [])}
    dims = [d for d in profile.get("dimensions", []) if d["scope"].startswith("w-")]
    per_workload = {d["dimension"] for d in dims if d["scope"] in workload_ids}
    shared = [d for d in dims if d["scope"] not in workload_ids and d["dimension"] not in per_workload]
    out = []
    for w in workloads:
        wid = w["id"]
        own = [d for d in dims if d["scope"] == wid]
        if w["kind"] == "reverse-proxy" and not any(d["dimension"] == "A1" for d in own):
            # 프록시는 S2 프로필 행이 없다. 종류(A1)는 인벤토리 워크로드 kind에서 온다(CAP-PROXY-001)
            ep = w.get("entrypoint") or {}
            own = own + [{"dimension": "A1", "scope": wid, "value": [PROXY_A1], "source": "detector",
                          "confidence": "high",
                          "evidence": [{"path": ep["path"], "line": ep.get("line")}] if ep.get("path") else []}]
        current = sorted({c["component"] for c in inventory.get("current_components", [])
                          if c["scope"] == wid and _family(c["component"]) in COMPUTE_FAMILIES})
        scaling = w.get("scaling") or {}
        min_replicas = scaling.get("min") if isinstance(scaling.get("min"), int) else 1
        out.append(Scope(id=wid, kind="compute", dims=own + shared, members=[wid], current=current,
                         workload_kind=w["kind"], min_replicas=max(1, min_replicas)))
    return out


def _vendor(component_id: str) -> str:
    return component_id.split(":", 1)[-1].split("/", 1)[0]


def is_baas(component_id: str, components: dict[str, dict]) -> bool:
    """엔진만 알려진 저장소도, 능력 표에 있는 클라우드의 관리형도 아닌 데이터 구성 요소(BaaS)."""
    clouds = {c.get("cloud") for c in components.values()}
    vendor = _vendor(component_id)
    return vendor not in PLAIN_VENDORS and vendor not in clouds


def data_scopes(inventory: dict, profile: dict, components: dict[str, dict]) -> list[Scope]:
    """엔진(postgres|redis|sqlite)을 알 수 있는 저장소 범위와, 현재 구성 요소가 BaaS인 범위(그대로 둔다)."""
    out = []
    for ds in sorted(inventory.get("datastores", []), key=lambda d: d["id"]):
        current = sorted({c["component"] for c in inventory.get("current_components", [])
                          if c["scope"] == ds["id"] and _family(c["component"]) in DATA_FAMILIES})
        dims = [d for d in profile.get("dimensions", []) if d["scope"] == ds["id"]]
        baas = [c for c in current if is_baas(c, components)]
        if baas:
            out.append(Scope(id=ds["id"], kind="data", dims=dims, members=[ds["id"]], current=baas,
                             engine=engine_of(baas[0], components), external=True))
            continue
        engines = sorted({e for e in (engine_of(c, components) for c in current) if e in ENGINE_FAMILY})
        if not engines:
            continue
        out.append(Scope(id=ds["id"], kind="data", dims=dims, members=[ds["id"]],
                         current=current, engine=engines[0]))
    return out


def candidates_for(scope: Scope, components: dict[str, dict]) -> list[str]:
    """범위와 같은 계열의 후보(능력 표에 있는 것) + 현재 구성 요소(능력 표에 있을 때)."""
    if scope.external:
        return sorted(set(scope.current))
    if scope.kind == "compute":
        ids = [cid for cid, c in components.items() if c.get("family", _family(cid)) in COMPUTE_FAMILIES]
    else:
        ids = [cid for cid, c in components.items()
               if c.get("family", _family(cid)) in DATA_FAMILIES and engine_of(cid, components) == scope.engine]
    return sorted(set(ids))
