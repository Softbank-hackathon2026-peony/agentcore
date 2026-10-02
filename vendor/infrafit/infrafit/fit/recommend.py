"""S4 추천(설계 §9, 계획 2 MVP): 조합 만들기와 사전식 순위.

조합 = 컴퓨트 1 × 데이터 범위마다 같은 클라우드의 관리형 대응.
SQLite는 영속 로컬 디스크가 있는 컴퓨트에서만 그대로 두고, 아니면 변형 "SQLite→관리형 Postgres"를 적용한다.
BaaS 저장소(Supabase·Firestore 등)는 바꾸지 않고 현재 구성 요소를 그대로 배정한다(external_scopes).
순위: 실현 불가 제외 → 합을 아는 후보가 먼저 → 월 비용 합(합이 null인 후보끼리는 모르는 비용 구성 요소 수, 그다음
아는 부분의 합; 부분합은 순위에만 쓰고 출력하지 않는다) → 모르는 판정 셀 수 → 운영 부담 → 설정 요구 수
→ 과금 방식(응답 밖 CPU가 필요 없으면 요청 기반이 먼저, A4·B3·워커가 있으면 응답 밖 CPU가 있는 쪽이 먼저) → ID.
비용을 모르는 구성 요소가 하나라도 있으면 합(monthly_baseline_usd)은 null이다. 0이나 부분합은 "무료"·"싸다"로
읽히므로 쓰지 않는다. 모르는 구성 요소는 cost.unknown_cost_components 에 적고 unknown_count 로도 센다.
scale-to-zero 플랫폼에서 인스턴스 고정 설정(단일 인스턴스·상시 실행)이 필요하면 바닥 비용 대신
COST.monthly_pinned_usd 를 쓰고, 그 값이 없으면 비용을 모른다.
결과(outcome): recommended | no_feasible | static_only(정적 프런트엔드만) | not_deployable(앱·정적 워크로드 없음).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from infrafit.fit.engine import cap_entry, cap_value, evaluate, match_when, source_of
from infrafit.fit.matrix import all_scopes, components_by_id
from infrafit.fit.scopes import Scope, _family

SQLITE_TRANSFORM = "TF-SQLITE-TO-MANAGED-POSTGRES"
# 연구 문서(docs/research/capabilities/*.md)의 확인일. 능력 출처에 checked_at이 있으면 그것을 쓴다.
PRICE_SNAPSHOT_DEFAULT = "2026-10-01"
OPS_ORDER = {"low": 0, "medium": 1, "high": 2}
RESULT_ORDER = {"feasible": 0, "feasible_with_config": 1, "unknown": 2, "infeasible": 3}
# 이 능력 키에서 설정 이름을 가져오는 규칙이 요구한 설정은 인스턴스를 붙잡아 둔다(scale-to-zero가 꺼진다)
PIN_CONFIG_KEYS = ("CP.single_instance_config", "CP.always_on_config")
# 응답 밖 CPU가 필요한 프로필(과금 방식 동점 깨기): 응답 후 작업, 앱 안 스케줄러, 상시 워커·정기 작업
NEEDS_BACKGROUND = {"any": [{"dimension": "A4", "equals": "있음"}, {"dimension": "B3", "equals": "있음"},
                            {"dimension": "A1", "in": ["워커", "정기 작업"]}]}
STATIC_KINDS = ("static-frontend",)


def ops_of(component: dict | None) -> str | None:
    value = (component or {}).get("ops_burden")
    if isinstance(value, dict):
        value = value.get("value")
    return value if value in OPS_ORDER else None


def cost_of(component: dict | None, key: str = "COST.monthly_floor_usd"):
    """(월 비용, 출처 항목) 또는 MISSING이면 (None, None)."""
    entry = cap_entry(component, key)
    if not isinstance(entry, dict) or not isinstance(entry.get("value"), (int, float)):
        return None, None
    return float(entry["value"]), entry


def agentcore_target(candidate: dict, capabilities: list[dict]) -> str | None:
    """후보 배정의 컴퓨트 구성 요소에서 AgentCore 대상 id(`target`)를 꺼낸다."""
    components = components_by_id(capabilities)
    for cid in sorted(candidate["assignment"].values()):
        if _family(cid) == "cp" and components.get(cid, {}).get("target"):
            return components[cid]["target"]
    return None


def _cost_key(total) -> float:
    """순위용: 합을 모르면(null) 아는 어떤 합보다 뒤."""
    return float("inf") if total is None else total


@dataclass
class Combo:
    compute: str
    assignment: dict[str, str] = field(default_factory=dict)
    cells: list[dict] = field(default_factory=list)
    transforms: list[str] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)   # Rejected.reasons 항목
    external: list[str] = field(default_factory=list)   # 현재 BaaS를 그대로 둔 데이터 범위

    def violations(self) -> list[dict]:
        return [v for c in self.cells for v in c["violations"]]


def _mentions(value, scope: Scope) -> bool:
    if isinstance(value, list):
        return any(_mentions(v, scope) for v in value)
    if isinstance(value, dict):
        return any(_mentions(v, scope) for v in value.values())
    return isinstance(value, str) and (scope.id in value or "sqlite" in value.lower())


def _strip_sqlite_b2(dims: list[dict], sqlite_scopes: list[Scope], inventory: dict) -> list[dict]:
    """변형 적용 후 프로필: SQLite 때문에 생긴 B2(로컬 디스크 상태) 값을 뺀다."""
    ds_evidence = {(e["path"], e.get("line")) for d in inventory.get("datastores", [])
                   if d["id"] in {s.id for s in sqlite_scopes} for e in d.get("evidence") or []}
    out = []
    for d in dims:
        if d["dimension"] != "B2":
            out.append(d)
            continue
        value = d.get("value")
        if isinstance(value, dict):  # S2 형식 {value: 있음|없음, kinds: [...]}: SQLite 종류만 뺀다
            kinds = list(value.get("kinds") or [])
            rest = [k for k in kinds if not any(_mentions(k, s) for s in sqlite_scopes)]
            if len(rest) == len(kinds):
                out.append(d)
            elif rest:
                out.append({**d, "value": {**value, "kinds": rest}})
            continue
        if isinstance(value, list):
            rest = [v for v in value if not any(_mentions(v, s) for s in sqlite_scopes)]
            if rest and len(rest) != len(value):
                out.append({**d, "value": rest})
                continue
            if not rest:
                continue
        if any(_mentions(value, s) for s in sqlite_scopes):
            continue
        ev = {(e["path"], e.get("line")) for e in d.get("evidence") or []}
        if ev and ev <= ds_evidence:
            continue
        out.append(d)
    return out


class Recommender:
    def __init__(self, inventory: dict, profile: dict, fit: dict, capabilities: list[dict], rules: list[dict]):
        self.inventory = inventory
        self.rules = rules
        self.components = components_by_id(capabilities)
        scopes = all_scopes(inventory, profile, self.components)
        self.app = [s for s in scopes if s.kind == "compute"]
        self.data = [s for s in scopes if s.kind == "data"]
        self.sqlite = [s for s in self.data if s.engine == "sqlite"]
        self.cells = {(c["scope"], c["candidate"]): c for c in fit["matrix"]}
        self.transform_cells: dict[tuple, dict] = {}
        self.pin_rules = {r["id"] for r in rules if r.get("config_from") in PIN_CONFIG_KEYS}
        self.needs_background = any(match_when(NEEDS_BACKGROUND, s.dims) for s in self.app)

    # ----- 선택 -----
    def _cell(self, scope: Scope, cid: str, transformed: bool = False) -> dict:
        if not transformed and (scope.id, cid) in self.cells:
            return self.cells[(scope.id, cid)]
        dims = scope.dims
        if transformed and scope.kind == "compute":
            dims = _strip_sqlite_b2(dims, self.sqlite, self.inventory)
        cell = evaluate(scope.id, scope.kind, cid, self.components.get(cid), dims, self.rules,
                        is_current=cid in scope.current).to_dict()
        if transformed:
            self.transform_cells[(scope.id, cid)] = cell
        return cell

    def _equivalent(self, scope: Scope, engine: str, cloud: str | None, transformed: bool):
        """같은 클라우드, 같은 엔진의 관리형 구성 요소 중 가장 나은 것(판정 → 비용 → 부담 → ID)."""
        options = sorted(cid for cid, c in self.components.items()
                         if _family(cid) in ("ds", "ca") and c.get("cloud") == cloud and cloud is not None
                         and cap_value(c, "DS.engine") == engine)
        scored = []
        for cid in options:
            cell = self._cell(scope, cid, transformed)
            cost, _ = cost_of(self.components[cid])
            scored.append(((RESULT_ORDER[cell["result"]], cost is None, cost or 0.0,
                            OPS_ORDER.get(ops_of(self.components[cid]), 3), cid), cid, cell))
        scored.sort(key=lambda x: x[0])
        return (scored[0][1], scored[0][2]) if scored else (None, None)

    def combo(self, cid: str) -> Combo:
        compute = self.components[cid]
        cloud = compute.get("cloud")
        combo = Combo(compute=cid)
        disk = cap_value(compute, "CP.persistent_local_disk") is True
        transformed = bool(self.sqlite) and not disk
        if transformed:
            combo.transforms.append(SQLITE_TRANSFORM)
        for scope in self.app:
            combo.assignment[scope.id] = cid
            combo.cells.append(self._cell(scope, cid, transformed))
        for scope in self.data:
            if scope.external:
                keep = scope.current[0]
                combo.assignment[scope.id] = keep
                combo.external.append(scope.id)
                if (scope.id, keep) in self.cells:
                    combo.cells.append(self.cells[(scope.id, keep)])
                continue
            if scope.engine == "sqlite" and disk:
                keep = scope.current[0]
                combo.assignment[scope.id] = keep
                if (scope.id, keep) in self.cells:
                    combo.cells.append(self.cells[(scope.id, keep)])
                continue
            engine = "postgres" if scope.engine == "sqlite" else scope.engine
            chosen, cell = self._equivalent(scope, engine, cloud, transformed and scope.engine == "sqlite")
            if chosen is None:
                combo.blocked.append({"type": "violation",
                                      "detail": f"{scope.id}: {cloud} 클라우드에 {engine} 관리형 대응이 능력 표에 없다"})
                continue
            combo.assignment[scope.id] = chosen
            combo.cells.append(cell)
        return combo

    # ----- 후보 -----
    def _pinned(self, combo: Combo) -> bool:
        """scale-to-zero 컴퓨트인데 인스턴스를 붙잡아 두는 설정이 필요한가."""
        if cap_value(self.components.get(combo.compute), "CP.scale_to_zero") is not True:
            return False
        app = {s.id for s in self.app}
        return any(req["rule"] in self.pin_rules for c in combo.cells if c["scope"] in app
                   for req in c["requires_config"])

    def _mode_key(self, compute: str) -> int:
        """과금 방식 동점 깨기: 응답 밖 CPU 유무가 프로필의 필요와 맞으면 0."""
        has_bg = cap_value(self.components.get(compute), "CP.cpu_after_response") is True
        return 0 if has_bg == self.needs_background else 1

    def _candidate(self, combo: Combo) -> dict:
        used = sorted(set(combo.assignment.values()))
        breakdown, total, unknown, snapshots, unknown_cost = [], 0.0, 0, [], []
        pinned = self._pinned(combo)
        for cid in used:
            key = "COST.monthly_pinned_usd" if pinned and cid == combo.compute else "COST.monthly_floor_usd"
            cost, entry = cost_of(self.components.get(cid), key)
            if cost is None:
                unknown += 1
                unknown_cost.append(cid)
                continue
            total += cost
            item = {"component": cid, "item": "monthly_pinned" if key.endswith("pinned_usd") else "monthly_floor",
                    "monthly_usd": cost, "price_source": source_of(entry)}
            breakdown.append(item)
            if item["price_source"].get("checked_at"):
                snapshots.append(item["price_source"]["checked_at"])
        unknown_cells = sum(1 for c in combo.cells if c["result"] == "unknown")
        unknown += unknown_cells
        ops = [o for o in (ops_of(self.components.get(cid)) for cid in used) if o]
        current = {s.id: s.current for s in self.app + self.data}
        is_current = all(cid in current.get(sid, []) for sid, cid in combo.assignment.items())
        if combo.transforms:
            effort = "medium"
        elif is_current:
            effort = "none"
        else:
            effort = "small"
        return {
            "id": "", "rank": 0,
            "assignment": dict(sorted(combo.assignment.items())),
            # 하나라도 모르면 합은 null(0이나 부분합이 무료·저렴으로 읽히지 않게)
            "cost": {"monthly_baseline_usd": None if unknown_cost else round(total, 4),
                     "breakdown": breakdown, "unknown_cost_components": unknown_cost,
                     "price_snapshot": max(snapshots) if snapshots else PRICE_SNAPSHOT_DEFAULT},
            "unknown_count": unknown,
            # 운영 부담 값이 하나도 없으면 보수적으로 high
            "ops_burden": max(ops, key=OPS_ORDER.get) if ops else "high",
            "human_steps": 0,
            "change_effort": effort,
            "is_current": is_current,
            "paths": [], "cross_scope_violations": [], "sizing": [],
            "transforms": list(combo.transforms),
            "external_scopes": sorted(combo.external),
            # 합이 null인 후보끼리는 모르는 구성 요소 수 → 아는 부분의 합으로 가른다(출력에는 쓰지 않는다)
            "_sort": (unknown_cells, sum(len(c["requires_config"]) for c in combo.cells),
                      self._mode_key(combo.compute), len(unknown_cost), round(total, 4)),
        }

    def run(self) -> dict:
        combos = [self.combo(cid) for cid in sorted(self.components)
                  if _family(cid) == "cp" and self.app]
        feasible, rejected = [], []
        for combo in combos:
            violations = combo.violations()
            if violations or combo.blocked:
                reasons = list(combo.blocked) + [
                    {"type": "violation", "detail": f"{v['rule']}: {v.get('message', '')}", "violation": v}
                    for v in violations]
                rejected.append({"id": combo.compute, "reasons": reasons})
            else:
                feasible.append(self._candidate(combo))
        feasible.sort(key=lambda c: (c["cost"]["monthly_baseline_usd"] is None, c["_sort"][3],
                                     _cost_key(c["cost"]["monthly_baseline_usd"]), c["_sort"][4], c["_sort"][0],
                                     OPS_ORDER[c["ops_burden"]], c["_sort"][1], c["_sort"][2],
                                     sorted(c["assignment"].items())))
        for i, cand in enumerate(feasible, start=1):
            cand["id"], cand["rank"] = f"C{i}", i
            del cand["_sort"]
        outcome, detail = self._outcome(feasible)
        no_feasible = None
        if outcome == "no_feasible":
            no_feasible = {"blocking": [r["violation"] for rej in rejected for r in rej["reasons"]
                                        if "violation" in r],
                           "suggestions": [],
                           "enabling_transforms": []}
        used_tf = any(SQLITE_TRANSFORM in c.transforms for c in combos)
        transforms = []
        if used_tf:
            transforms.append({
                "id": SQLITE_TRANSFORM, "title": "SQLite→관리형 Postgres",
                "applies_to": [s.id for s in self.sqlite], "effects": [], "adds_scopes": [],
                "code_effort": "medium",
                "because": "SQLite 파일은 영속 로컬 디스크가 없는 컴퓨트에서 유지되지 않는다",
            })
        transform_fits = []
        if self.transform_cells:
            transform_fits.append({"transform_set": [SQLITE_TRANSFORM],
                                   "matrix": [self.transform_cells[k] for k in sorted(self.transform_cells)]})
        return {
            "candidates": feasible,
            "recommended": feasible[0]["id"] if feasible else None,
            "rejected": sorted(rejected, key=lambda r: r["id"]),
            # 아래는 MVP에서 계산하지 않는다: 민감도 없음, 시나리오 요약은 0, 관점 없음
            "sensitivity": [],
            "scenario_summary": {"D": 0, "T": 0, "U": 0, "C": 0, "S": 0, "O": 0},
            "no_feasible": no_feasible,
            "transforms": transforms,
            "transform_fits": transform_fits,
            "perspectives": None,
            "outcome": outcome,
            "outcome_detail": detail,
        }

    def _outcome(self, feasible: list[dict]) -> tuple[str, dict | None]:
        if feasible:
            return "recommended", None
        if self.app:
            return "no_feasible", None
        workloads = self.inventory.get("workloads", [])
        static = sorted(w["id"] for w in workloads if w["kind"] in STATIC_KINDS)
        if not static:
            return "not_deployable", {
                "message": "앱·정적 프런트엔드 워크로드가 없어 배포할 대상이 없다", "current": []}
        current: list[dict] = []
        for c in sorted(self.inventory.get("current_components", []),
                        key=lambda c: (c["scope"], c["component"], c.get("label", ""))):
            if c["scope"] not in static:
                continue
            item = {"scope": c["scope"], "component": c["component"]}
            if c.get("label"):
                item["label"] = c["label"]
            if item not in current:
                current.append(item)
        where = ", ".join(f"{c['scope']}: {c.get('label') or c['component']}" for c in current) or "현재 호스팅 미확인"
        return "static_only", {
            "message": f"정적 프런트엔드만 있다({where}). 서버 컴퓨트가 필요 없어 컴퓨트를 고르지 않는다",
            "current": current}


def build_recommendation(inventory: dict, profile: dict, fit: dict,
                         capabilities: list[dict], rules: list[dict]) -> dict:
    return Recommender(inventory, profile, fit, capabilities, rules).run()

