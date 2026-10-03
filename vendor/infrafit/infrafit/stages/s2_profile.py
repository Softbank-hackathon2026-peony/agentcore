"""S2 프로필: 앱이 무엇을 요구하는가(설계 §7, 계획 2 작업 2). 결정적 탐지기만 쓴다(llm_used: false).

범위: 앱 워크로드(knowledge/profile_detectors.yaml `app_kinds`)마다 워크로드 행, 탐지된 값이 있는 엔드포인트 행,
그리고 앱 워크로드 전체를 합친 앱 집계 범위(`app_scope`, 기본 `w-app`; 워크로드 ID와 겹치면 `w-app.all`) 행.
앱 집계 행은 aggregated_from에 합친 워크로드 ID를 담는다. 가정(D2·G3 등)은 assumptions[]와 앱 집계 행에 둔다.
D2(평시 동시성)·D6(확장 요구)는 워크로드마다 인벤토리 scaling으로 정하고, 근거가 없으면 D2만 가정(낮음)이다.
batch_only: 앱 워크로드가 모두 batch이고 정적 프런트엔드가 없으면 value=true(사람이 실행하는 도구, S4 not_deployable).
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


def batch_only(inventory: dict, app_ids: list[str], cfg: dict | None = None) -> dict:
    """앱 워크로드가 하나 이상이고 모두 batch(일회성 실행)이며 정적 프런트엔드가 없으면 value=true."""
    cfg = cfg or kb.profile_detectors()
    by_id = {w["id"]: w for w in inventory["workloads"]}
    static = any(w["kind"] == "static-frontend" for w in inventory["workloads"])
    value = bool(app_ids) and not static and all(by_id[i]["kind"] == "batch" for i in app_ids)
    out = {"value": value, "workloads": app_ids if value else [],
           "evidence": [by_id[i]["entrypoint"] for i in app_ids] if value else []}
    if value:
        out["reason"] = (cfg.get("batch_only") or {}).get("reason", "")
    return out


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
    # 앱 범위가 없으면 앱에 대한 가정(D2·G3)도 두지 않는다
    for a in cfg.get("assumptions", []) if app_ids else []:
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
        "batch_only": batch_only(inventory, app_ids, cfg),
    }


def run_s2(ctx: RunContext, snap: Snapshot, inventory: dict) -> dict:
    started = now_iso()
    body = {k: v for k, v in inventory.items() if k != "meta"}
    h = input_hash("S2", inventory["meta"]["input_hash"], body, kb.kb_version(), code_version())
    cached = ctx.cached("S2", h)
    if cached is not None:
        return cached
    return ctx.write_stage("S2", build_profile(snap, inventory), input_hash=h, started_at=started)
