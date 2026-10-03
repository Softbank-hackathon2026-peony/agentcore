"""S4 서비스 유형별 순위(knowledge/ranking.yaml, 설계 2026-10-03-infrafit-service-type-ranking-design.md).

유형 판정, 기준 값 계산 도우미, 비용 동률, decided_by. 순위는 사전식이고 유형마다 기준 순서만 다르다.
"""

from __future__ import annotations

from infrafit.fit.engine import cap_value, dim_value, match_when

# 계산 함수가 있는 기준. ranking.yaml criteria 는 이 안에서만 고른다(kb_lint)
CRITERIA = ("certainty", "cost", "always_on", "request_headroom", "scaling", "data_safety", "config_burden")

DEFAULT_REASON = "근거 있는 차원 값이 어느 유형 조건에도 맞지 않음"


def _at(evidence) -> list[str]:
    out = []
    for e in evidence or []:
        if e.get("path"):
            out.append(f"{e['path']}:{e['line']}" if e.get("line") else e["path"])
    return out


def _hits(when: dict, dims: list[dict], datastores: list[dict]) -> list[dict] | None:
    """조건에 맞으면 근거 목록 [{dimension, value, at}], 아니면 None."""
    if "any" in when:
        found = [h for child in when["any"] if (h := _hits(child, dims, datastores)) is not None]
        return [x for h in found for x in h] if found else None
    if "datastores" in when:
        if bool(datastores) != bool(when["datastores"]):
            return None
        return [{"dimension": "datastores", "value": d["id"], "at": _at(d.get("evidence"))} for d in datastores]
    if "kinds" in when:
        rows = [d for d in dims if d["dimension"] == when["dimension"] and isinstance(d.get("value"), dict)
                and set(d["value"].get("kinds") or []) & set(when["kinds"])]
    else:
        rows = match_when(when, dims)
    if not rows:
        return None
    return [{"dimension": d["dimension"], "value": dim_value(d.get("value")), "at": _at(d.get("evidence"))}
            for d in rows]


def _dedupe(hits: list[dict]) -> list[dict]:
    out: list[dict] = []
    for h in hits:
        same = next((o for o in out if o["dimension"] == h["dimension"] and o["value"] == h["value"]), None)
        if same is None:
            out.append({**h, "at": list(dict.fromkeys(h["at"]))})
        else:
            same["at"] = list(dict.fromkeys(same["at"] + h["at"]))
    return out


def classify(dims: list[dict], datastores: list[dict], cfg: dict) -> dict:
    """앱 워크로드 차원 행(근거 있는 것만)과 데이터 저장소로 서비스 유형 하나를 고른다. 출력 `ranking` 블록."""
    usable = [d for d in dims if d.get("source") == "detector"]
    types = cfg["service_types"]
    matched = []
    for t in types:
        if t["when"] == "default":
            continue
        hits = _hits(t["when"], usable, datastores)
        if hits is not None:
            matched.append({"type": t["id"], "by": _dedupe(hits)})
    chosen = next((t for t in types if matched and t["id"] == matched[0]["type"]), None) \
        or next(t for t in types if t["when"] == "default")
    if not matched:
        coverage = "default"
    elif len(matched) == 1:
        coverage = "full"
    else:
        coverage = "partial"
    return {
        "service_type": chosen["id"], "label": chosen["label"], "coverage": coverage, "matched": matched,
        "unprioritized": [m["type"] for m in matched[1:]],
        "default_reason": None if matched else DEFAULT_REASON,
        "criteria_order": list(chosen["order"]), "why": chosen["why"], "refs": list(chosen["refs"]),
        "scope": dict(cfg["scope"]),
    }


A2_ORDER = ("1초 미만", "수십 초", "수 분", "그 이상")
# 필요 등급 → 한 단계 여유 있는 요청 상한(초). None = 상한이 없어야 함(CP.platform_request_timeout false)
HEADROOM_NEED = {"1초 미만": 60, "수십 초": 600, "수 분": None, "그 이상": None}
SCALING_KINDS = ("web", "realtime")


def a2_of(dims: list[dict]) -> str | None:
    """근거 있는(detector) A2 값 중 가장 높은 등급. 없으면 None."""
    vals = [dim_value(d.get("value")) for d in dims if d["dimension"] == "A2" and d.get("source") == "detector"]
    vals = [v for v in vals if v in A2_ORDER]
    return max(vals, key=A2_ORDER.index) if vals else None


def a2_need(dims: list[dict]) -> str | None:
    """처리 시간 필요 등급. 근거 있는 A2 가 없고 근거 있는 E2 에 llm-api 가 있으면 "수십 초"(LLM 호출, W-011)."""
    a2 = a2_of(dims)
    if a2 is not None:
        return a2
    for d in dims:
        if d["dimension"] == "E2" and d.get("source") == "detector" and isinstance(d.get("value"), dict) \
                and "llm-api" in (d["value"].get("kinds") or []):
            return "수십 초"
    return None


def lacks_always_on(component: dict | None) -> bool:
    return cap_value(component, "CP.always_on") is not True


def lacks_headroom(component: dict | None, a2: str | None) -> bool:
    """필요 등급보다 한 단계 위 상한이 없는가. 근거 있는 A2 가 없으면 따지지 않는다. 상한을 모르면 여유 없음."""
    if a2 not in HEADROOM_NEED:
        return False
    if cap_value(component, "CP.platform_request_timeout") is False:
        return False
    need = HEADROOM_NEED[a2]
    if need is None:
        return True
    limit = cap_value(component, "CP.max_request_seconds")
    return not (isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit >= need)


def lacks_scaling(component: dict | None) -> bool:
    return cap_value(component, "CP.horizontal_scaling") is not True


def unsafe_data(cid: str, component: dict | None) -> bool:
    """VM 안 컨테이너 저장소나 로컬 SQLite 인가(관리형·BaaS 가 아님)."""
    return (cap_value(component, "DS.colocated_vm") is True or cap_value(component, "DS.engine") == "sqlite"
            or cid.startswith("ds:local/sqlite"))


def cost_value(total: float | None, unknown_n: int, partial: float, cheapest: float | None, ratio: float) -> tuple:
    """비용 기준 값. 합을 모르면 (True, 모르는 수, 아는 부분합). 알면 최저 합 × (1 + ratio) 이하는 0(동률)."""
    if total is None:
        return (True, unknown_n, partial)
    tied = cheapest is not None and total <= cheapest * (1 + ratio)
    return (False, 0.0 if tied else total, 0)


def sort_key(values: dict, order: list[str]) -> tuple:
    return tuple(values[c] for c in order)


def decided_by(rows: list[tuple[str, dict]], order: list[str]) -> list[dict | None]:
    """순위 순서의 (id, 기준 값)마다 바로 다음 후보와 처음 갈린 기준. 마지막은 None.

    기준 값에 "unknown_count"(모르는 셀 수, 유형 기준 뒤의 동률 깨기)가 있으면 order 다음에 비교한다.
    """
    out: list[dict | None] = []
    for i, (_, vals) in enumerate(rows):
        if i + 1 == len(rows):
            out.append(None)
            continue
        nid, nvals = rows[i + 1]
        keys = [*order, "unknown_count"]
        out.append({"criterion": next((c for c in keys if vals.get(c) != nvals.get(c)), "name"), "over": nid})
    return out
