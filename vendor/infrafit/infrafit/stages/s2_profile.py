"""S2 프로필: 앱이 무엇을 요구하는가(설계 §7, 계획 2 작업 2). 결정적 탐지기만 쓴다(llm_used: false).

범위: 앱 워크로드(knowledge/profile_detectors.yaml `app_kinds`)마다 워크로드 행, 탐지된 값이 있는 엔드포인트 행,
그리고 앱 워크로드 전체를 합친 앱 집계 범위(`app_scope`, 기본 `w-app`; 워크로드 ID와 겹치면 `w-app.all`) 행.
앱 집계 행은 aggregated_from에 합친 워크로드 ID를 담는다. 가정(D2·G3 등)은 assumptions[]와 앱 집계 행에 둔다.
"""

from __future__ import annotations

from infrafit import kb
from infrafit.profile.detectors import observe
from infrafit.profile.values import app_rows, workload_rows
from infrafit.repo import Snapshot
from infrafit.run import RunContext, code_version, input_hash, now_iso


def app_workload_ids(inventory: dict, cfg: dict | None = None) -> list[str]:
    cfg = cfg or kb.profile_detectors()
    kinds = set(cfg["app_kinds"])
    return sorted(w["id"] for w in inventory["workloads"] if w["kind"] in kinds)


def app_scope_id(inventory: dict, cfg: dict | None = None) -> str:
    cfg = cfg or kb.profile_detectors()
    base = cfg["app_scope"]
    taken = {w["id"] for w in inventory["workloads"]}
    return base if base not in taken else base + ".all"


def build_profile(snap: Snapshot, inventory: dict, cfg: dict | None = None) -> dict:
    cfg = cfg or kb.profile_detectors()
    dims = cfg["dimensions"]
    app_ids = app_workload_ids(inventory, cfg)
    workloads = [w for w in inventory["workloads"] if w["id"] in app_ids]
    endpoints = [e for e in inventory["endpoints"] if e["workload"] in app_ids]
    obs = observe(snap, inventory, cfg, app_ids)
    ep_rows, w_rows = workload_rows(dims, workloads, endpoints, obs)
    dimensions = ep_rows + w_rows
    assumptions: list[dict] = []
    used_keys = {r["assumption_key"] for r in w_rows if r.get("assumption_key")}
    for key in sorted(used_keys):
        assumptions.append({"key": key, "value": dims[key]["default"]["value"],
                            "reason": dims[key]["default"]["reason"], "origin": "domain-default"})
    if app_ids:
        scope = app_scope_id(inventory, cfg)
        dimensions += app_rows(dims, scope, w_rows)
        for a in cfg.get("assumptions", []):
            dimensions.append({"dimension": a["key"], "scope": scope, "value": a["value"], "source": "assumption",
                               "confidence": "low", "assumption_key": a["key"], "evidence": [],
                               "aggregated_from": app_ids})
    for a in cfg.get("assumptions", []):
        assumptions.append({"key": a["key"], "value": a["value"], "reason": a["reason"], "origin": "domain-default"})
    order = {s: i for i, s in enumerate([e["id"] for e in endpoints] + app_ids)}
    dimensions.sort(key=lambda r: (order.get(r["scope"], len(order)), r["scope"], r["dimension"]))
    return {
        "domain": {"value": "unknown", "votes": []},
        "dimensions": dimensions,
        "assumptions": sorted(assumptions, key=lambda a: a["key"]),
        "dropped_inferences": [],
        "llm_used": False,
        "resolutions": [],
    }


def run_s2(ctx: RunContext, snap: Snapshot, inventory: dict) -> dict:
    started = now_iso()
    body = {k: v for k, v in inventory.items() if k != "meta"}
    h = input_hash("S2", inventory["meta"]["input_hash"], body, kb.kb_version(), code_version())
    cached = ctx.cached("S2", h)
    if cached is not None:
        return cached
    return ctx.write_stage("S2", build_profile(snap, inventory), input_hash=h, started_at=started)
