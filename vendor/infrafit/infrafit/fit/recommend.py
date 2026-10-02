"""S4 추천(설계 §9, 계획 2b): 배포 형태(토폴로지) × 클라우드 조합과 사전식 순위.

compute 범위는 워크로드마다 하나다(scopes.app_scopes). 조합은 토폴로지 세 가지 × 클라우드:
- vm-compose: 워크로드 전부를 VM 한 대(EC2·Compute Engine)에. 데이터는 같은 VM 안 컨테이너
  (DS.colocated_vm: ds:vm/compose-postgres, ca:vm/compose-redis, SQLite는 VM 디스크에 그대로) 변형과
  같은 클라우드 관리형 변형(`/managed-data`) 두 가지. 비용 = VM 바닥 비용(+ 관리형 데이터 바닥 비용).
  확장 요구(D6 고정 다중·자동 확장)는 CAP-SCALE-001(CP.horizontal_scaling false)로 탈락한다.
- services: 워크로드마다 같은 클라우드의 서비스형 플랫폼(Lambda·Cloud Run 두 과금·ECS) 중 규칙을 통과한 가장 나은 것
  (근거 있는 모름 없음 → 비용을 앎 → 낮은 비용 → 모르는 셀 → 운영 부담 → 설정 수 → 과금 방식 → ID).
- kubernetes: 워크로드 전부를 클러스터 하나(GKE Autopilot·EKS)에.
데이터 범위: BaaS(Supabase·Firestore 등)는 그대로(external_scopes). SQLite는 그 조합의 compute가 모두 영속 로컬
디스크를 가질 때만 그대로 두고, 아니면 변형 "SQLite→관리형 Postgres"를 적용한다.

비용(월, 서울):
- 레플리카 수 = 워크로드의 scaling.min(없으면 1). 구성 요소마다 그 구성 요소에 놓인 레플리카 합 N으로 계산한다.
  Lambda(NO_REPLICA_TARGETS)는 레플리카 개념이 없어(실행 환경당 요청 하나, post-response-work.md §4 D13) 최소 레플리카를
  비용에 곱하지 않는다: 워크로드마다 1로 센다(바닥 비용이 요청 과금이라 0)이고 고정(pinned) 비용도 쓰지 않는다.
- 바닥 비용(COST.monthly_floor_usd)은 인스턴스 1개 기준이고 공유 고정비(ECS의 ALB·공인 IP, 클러스터 요금·Ingress)를 이미
  포함한다. N > 1이면 바닥 비용 + COST.per_replica_usd × (N − 1)이고, 단가가 없으면 비용을 모른다(공유 고정비를 두 번
  세지 않도록 바닥 비용 × N을 쓰지 않는다). 바닥 비용이 0이면(요청 과금) 0. vm-compose는 VM 한 대라 N = 1.
- services의 scale-to-zero 플랫폼에서 인스턴스 고정 설정(단일 인스턴스·상시 실행)이 필요하거나 저장소가 최소 레플리카 ≥ 2를
  밝힌 워크로드는 바닥 비용 대신 COST.monthly_pinned_usd × 레플리카(인스턴스당 값)를 쓰고, 그 값이 없으면 비용을 모른다.
- 비용을 모르는 구성 요소가 하나라도 있으면 합(monthly_baseline_usd)은 null이다. 0이나 부분합은 쓰지 않는다.
순위: 실현 불가 제외 → 근거 있는 차원(source=detector)에서 나온 모름 셀이 없는 후보가 먼저(가정 값에서 나온 모름은 내리지
않는다) → 합을 아는 후보가 먼저 → 낮은 비용(합이 null인 후보끼리는 모르는 비용 구성 요소 수, 그다음 아는 부분의 합;
부분합은 순위에만 쓴다) → 모르는 셀·비용 수 → 운영 부담 → 설정 요구 수 → 조합 이름.
결과(outcome): recommended | no_feasible | static_only(정적 프런트엔드만) | not_deployable(앱·정적 워크로드 없음, 또는
profile.batch_only: 사람이 실행하는 도구).
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
TOPOLOGIES = ("vm-compose", "services", "kubernetes")
# 레플리카 개념이 없는 대상: 요청마다 실행 환경을 늘리고 줄인다(D13). 비용에 최소 레플리카를 곱하지 않는다
NO_REPLICA_TARGETS = ("aws_lambda",)
# AgentCore 대상 → 토폴로지. 능력 표 구성 요소에 `topology`가 있으면 그것이 먼저, 나머지 대상은 services.
TOPOLOGY_BY_TARGET = {"aws_ec2": "vm-compose", "gcp_compute_engine": "vm-compose",
                      "aws_eks": "kubernetes", "gcp_gke": "kubernetes"}


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


def topology_of(component: dict | None) -> str:
    component = component or {}
    return component.get("topology") or TOPOLOGY_BY_TARGET.get(component.get("target")) or "services"


def agentcore_target(candidate: dict, capabilities: list[dict]) -> str | None:
    """후보 배정의 컴퓨트 구성 요소에서 AgentCore 대상 id(`target`)를 꺼낸다(첫 compute 구성 요소)."""
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
    name: str
    topology: str
    cloud: str | None
    assignment: dict[str, str] = field(default_factory=dict)
    cells: list[dict] = field(default_factory=list)
    evidence_unknown: int = 0                           # 근거 있는 차원에서 나온 모름 셀 수
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
        self.profile = profile
        self.rules = rules
        self.components = components_by_id(capabilities)
        scopes = all_scopes(inventory, profile, self.components)
        self.app = [s for s in scopes if s.kind == "compute"]
        self.data = [s for s in scopes if s.kind == "data"]
        self.sqlite = [s for s in self.data if s.engine == "sqlite"]
        self.fit_cells = {(c["scope"], c["candidate"]) for c in fit["matrix"]}
        self.cache: dict[tuple, tuple[dict, bool]] = {}
        self.transform_cells: dict[tuple, dict] = {}
        self.pin_rules = {r["id"] for r in rules if r.get("config_from") in PIN_CONFIG_KEYS}

    # ----- 셀 -----
    def _cell(self, scope: Scope, cid: str, transformed: bool = False) -> tuple[dict, bool]:
        """(FitCell, 근거 있는 모름인가). 변형 셀은 transform_fits에 남긴다."""
        key = (scope.id, cid, transformed)
        if key not in self.cache:
            dims = scope.dims
            if transformed and scope.kind == "compute":
                dims = _strip_sqlite_b2(dims, self.sqlite, self.inventory)
            cell = evaluate(scope.id, scope.kind, cid, self.components.get(cid), dims, self.rules,
                            is_current=cid in scope.current)
            self.cache[key] = (cell.to_dict(), cell.evidence_unknown)
        cell, ev = self.cache[key]
        if transformed:
            self.transform_cells[(scope.id, cid)] = cell
        return cell, ev

    def _add(self, combo: Combo, scope: Scope, cid: str, transformed: bool) -> dict:
        cell, ev = self._cell(scope, cid, transformed)
        combo.assignment[scope.id] = cid
        combo.cells.append(cell)
        combo.evidence_unknown += int(ev and cell["result"] == "unknown")
        return cell

    # ----- 비용 -----
    def _replicas(self, scope: Scope, cid: str) -> int:
        """비용에 쓸 레플리카 수: 저장소의 최소 레플리카, 레플리카 개념이 없는 대상(Lambda)은 1."""
        if (self.components.get(cid) or {}).get("target") in NO_REPLICA_TARGETS:
            return 1
        return scope.min_replicas

    def _needs_pin(self, scope: Scope, cid: str, cell: dict) -> bool:
        """services의 scale-to-zero 플랫폼에서 인스턴스를 붙잡아 둬야 하는가(고정 설정 요구 또는 최소 레플리카 ≥ 2)."""
        if (self.components.get(cid) or {}).get("target") in NO_REPLICA_TARGETS:
            return False
        if cap_value(self.components.get(cid), "CP.scale_to_zero") is not True:
            return False
        if scope.min_replicas >= 2:
            return True
        return any(req["rule"] in self.pin_rules for req in cell["requires_config"])

    def _compute_cost(self, cid: str, units: int, pinned_units: int) -> tuple[float | None, list[dict]]:
        """구성 요소 하나의 월 비용(모르면 None)과 내역. units = 바닥 비용으로 셀 레플리카 수, pinned_units = 고정 레플리카 수."""
        comp = self.components.get(cid)
        total, items = 0.0, []
        if pinned_units:
            cost, entry = cost_of(comp, "COST.monthly_pinned_usd")
            if cost is None:
                return None, []
            items.append({"component": cid, "item": "monthly_pinned" + (f" ×{pinned_units}" if pinned_units > 1 else ""),
                          "monthly_usd": round(cost * pinned_units, 4), "price_source": source_of(entry)})
            total += cost * pinned_units
        if units:
            floor, entry = cost_of(comp)
            if floor is None:
                return None, []
            items.append({"component": cid, "item": "monthly_floor", "monthly_usd": floor,
                          "price_source": source_of(entry)})
            total += floor
            if units > 1 and floor > 0:
                per, per_entry = cost_of(comp, "COST.per_replica_usd")
                if per is None:
                    return None, []
                items.append({"component": cid, "item": f"per_replica ×{units - 1}",
                              "monthly_usd": round(per * (units - 1), 4), "price_source": source_of(per_entry)})
                total += per * (units - 1)
        return round(total, 4), items

    def _option_cost(self, scope: Scope, cid: str, cell: dict) -> float | None:
        """services 워크로드 하나를 이 플랫폼에 둘 때의 비용(플랫폼 고르기용)."""
        if self._needs_pin(scope, cid, cell):
            return self._compute_cost(cid, 0, scope.min_replicas)[0]
        return self._compute_cost(cid, self._replicas(scope, cid), 0)[0]

    def _mode_key(self, scope: Scope, compute: str) -> int:
        """과금 방식 동점 깨기: 응답 밖 CPU 유무가 그 워크로드의 필요와 맞으면 0."""
        has_bg = cap_value(self.components.get(compute), "CP.cpu_after_response") is True
        return 0 if has_bg == bool(match_when(NEEDS_BACKGROUND, scope.dims)) else 1

    # ----- 데이터 -----
    def _data_options(self, engine: str, cloud: str | None, colocated: bool) -> list[str]:
        out = []
        for cid, c in self.components.items():
            if _family(cid) not in ("ds", "ca") or cap_value(c, "DS.engine") != engine:
                continue
            is_colocated = cap_value(c, "DS.colocated_vm") is True
            if colocated and is_colocated:
                out.append(cid)
            elif not colocated and not is_colocated and cloud is not None and c.get("cloud") == cloud:
                out.append(cid)
        return sorted(out)

    def _best_data(self, scope: Scope, engine: str, cloud: str | None, transformed: bool, colocated: bool):
        """같은 엔진의 VM 안(colocated) 또는 같은 클라우드 관리형 구성 요소 중 가장 나은 것(판정 → 비용 → 부담 → ID)."""
        scored = []
        for cid in self._data_options(engine, cloud, colocated):
            cell, _ = self._cell(scope, cid, transformed)
            cost, _ = cost_of(self.components[cid])
            scored.append(((RESULT_ORDER[cell["result"]], cost is None, cost or 0.0,
                            OPS_ORDER.get(ops_of(self.components[cid]), 3), cid), cid))
        scored.sort()
        return scored[0][1] if scored else None

    def _replaced_data(self, disk: bool) -> list[Scope]:
        """구성 요소를 새로 골라야 하는 데이터 범위(BaaS·그대로 두는 SQLite 제외)."""
        return [s for s in self.data if not s.external and not (s.engine == "sqlite" and disk)]

    def _assign_data(self, combo: Combo, disk: bool, transformed: bool, colocated: bool) -> None:
        for scope in self.data:
            if scope.external or (scope.engine == "sqlite" and disk):
                keep = scope.current[0]
                combo.assignment[scope.id] = keep
                if scope.external:
                    combo.external.append(scope.id)
                if (scope.id, keep) in self.fit_cells:
                    self._add(combo, scope, keep, False)
                continue
            engine = "postgres" if scope.engine == "sqlite" else scope.engine
            tf = transformed and scope.engine == "sqlite"
            chosen = self._best_data(scope, engine, combo.cloud, tf, True) if colocated else None
            if chosen is None:
                chosen = self._best_data(scope, engine, combo.cloud, tf, False)
            if chosen is None:
                combo.blocked.append({"type": "violation",
                                      "detail": f"{scope.id}: {combo.cloud} 클라우드에 {engine} 관리형 대응이 능력 표에 없다"})
                continue
            self._add(combo, scope, chosen, tf)

    # ----- 조합 -----
    def _whole(self, cid: str, topology: str, colocated: bool, name: str) -> Combo:
        """vm-compose·kubernetes: 워크로드 전부를 구성 요소 하나에."""
        compute = self.components[cid]
        combo = Combo(name=name, topology=topology, cloud=compute.get("cloud"))
        disk = cap_value(compute, "CP.persistent_local_disk") is True
        transformed = bool(self.sqlite) and not disk
        if transformed:
            combo.transforms.append(SQLITE_TRANSFORM)
        for scope in self.app:
            self._add(combo, scope, cid, transformed)
        self._assign_data(combo, disk, transformed, colocated)
        return combo

    def _choose(self, scope: Scope, options: list[str], transformed: bool):
        """services: 워크로드 하나에 둘 플랫폼. (선택 또는 None, 탈락 사유)"""
        scored, reasons = [], []
        for cid in options:
            cell, ev = self._cell(scope, cid, transformed)
            if cell["result"] == "infeasible":
                reasons += [{"type": "violation", "detail": f"{scope.id} × {cid} — {v['rule']}: {v.get('message', '')}",
                             "violation": v} for v in cell["violations"]]
                continue
            cost = self._option_cost(scope, cid, cell)
            scored.append(((ev and cell["result"] == "unknown", cost is None, cost or 0.0,
                            cell["result"] == "unknown", OPS_ORDER.get(ops_of(self.components[cid]), 3),
                            len(cell["requires_config"]), self._mode_key(scope, cid), cid), cid))
        scored.sort()
        return (scored[0][1] if scored else None), reasons

    def _services(self, cloud: str, options: list[str]) -> Combo:
        combo = Combo(name=f"services/{cloud}", topology="services", cloud=cloud)
        disk_of = {cid: cap_value(self.components[cid], "CP.persistent_local_disk") is True for cid in options}
        transformed = False
        chosen = {s.id: self._choose(s, options, False) for s in self.app}
        if self.sqlite and not all(c is not None and disk_of[c] for c, _ in chosen.values()):
            transformed = True
            chosen = {s.id: self._choose(s, options, True) for s in self.app}
            combo.transforms.append(SQLITE_TRANSFORM)
        for scope in self.app:
            cid, reasons = chosen[scope.id]
            if cid is None:
                combo.blocked += reasons or [{"type": "violation",
                                              "detail": f"{scope.id}: {cloud} 서비스형 플랫폼이 능력 표에 없다"}]
                continue
            self._add(combo, scope, cid, transformed)
        disk = not transformed and bool(self.sqlite)
        self._assign_data(combo, disk, transformed, False)
        return combo

    def combos(self) -> list[Combo]:
        if not self.app:
            return []
        out: list[Combo] = []
        by_topology: dict[str, list[str]] = {t: [] for t in TOPOLOGIES}
        for cid in sorted(self.components):
            if _family(cid) == "cp":
                by_topology.setdefault(topology_of(self.components[cid]), []).append(cid)
        for cid in by_topology["vm-compose"]:
            out.append(self._whole(cid, "vm-compose", True, f"vm-compose/{cid}"))
            disk = cap_value(self.components[cid], "CP.persistent_local_disk") is True
            if any(self._data_options("postgres" if s.engine == "sqlite" else s.engine, None, True)
                   for s in self._replaced_data(disk)):
                out.append(self._whole(cid, "vm-compose", False, f"vm-compose/{cid}/managed-data"))
        clouds = sorted({self.components[c].get("cloud") for c in by_topology["services"]} - {None})
        for cloud in clouds:
            out.append(self._services(cloud, [c for c in by_topology["services"]
                                              if self.components[c].get("cloud") == cloud]))
        for cid in by_topology["kubernetes"]:
            out.append(self._whole(cid, "kubernetes", False, f"kubernetes/{cid}"))
        return out

    # ----- 후보 -----
    def _cost(self, combo: Combo) -> tuple[float, list[dict], list[str]]:
        """(아는 부분의 합, 내역, 비용을 모르는 구성 요소)."""
        app = {s.id: s for s in self.app}
        cells = {c["scope"]: c for c in combo.cells}
        units: dict[str, int] = {}
        pinned: dict[str, int] = {}
        for sid, scope in app.items():
            cid = combo.assignment.get(sid)
            if cid is None:
                continue
            units.setdefault(cid, 0)
            pinned.setdefault(cid, 0)
            if combo.topology == "vm-compose":
                units[cid] = 1                       # VM 한 대
            elif combo.topology == "services" and self._needs_pin(scope, cid, cells[sid]):
                pinned[cid] += scope.min_replicas
            else:
                units[cid] += self._replicas(scope, cid)
        total, breakdown, unknown = 0.0, [], []
        for cid in sorted(units):
            cost, items = self._compute_cost(cid, units[cid], pinned[cid])
            if cost is None:
                unknown.append(cid)
                continue
            total += cost
            breakdown += items
        for cid in sorted({cid for sid, cid in combo.assignment.items() if sid not in app}):
            cost, entry = cost_of(self.components.get(cid))
            if cost is None:
                unknown.append(cid)
                continue
            total += cost
            breakdown.append({"component": cid, "item": "monthly_floor", "monthly_usd": cost,
                              "price_source": source_of(entry)})
        return round(total, 4), breakdown, unknown

    def _candidate(self, combo: Combo) -> dict:
        used = sorted(set(combo.assignment.values()))
        total, breakdown, unknown_cost = self._cost(combo)
        snapshots = [i["price_source"]["checked_at"] for i in breakdown if i["price_source"].get("checked_at")]
        unknown_cells = sum(1 for c in combo.cells if c["result"] == "unknown")
        ops = [o for o in (ops_of(self.components.get(cid)) for cid in used) if o]
        current = {s.id: s.current for s in self.app + self.data}
        is_current = all(cid in current.get(sid, []) for sid, cid in combo.assignment.items())
        if combo.transforms:
            effort = "medium"
        elif is_current:
            effort = "none"
        else:
            effort = "small"
        placement = [{"scope": s.id, "component": combo.assignment[s.id],
                      "target": self.components.get(combo.assignment[s.id], {}).get("target"),
                      "min_replicas": s.min_replicas} for s in self.app]
        configs = sum(len(c["requires_config"]) for c in combo.cells)
        return {
            "id": "", "rank": 0,
            "topology": combo.topology,
            "assignment": dict(sorted(combo.assignment.items())),
            "placement": placement,
            # 하나라도 모르면 합은 null(0이나 부분합이 무료·저렴으로 읽히지 않게)
            "cost": {"monthly_baseline_usd": None if unknown_cost else total,
                     "breakdown": breakdown, "unknown_cost_components": unknown_cost,
                     "price_snapshot": max(snapshots) if snapshots else PRICE_SNAPSHOT_DEFAULT},
            "unknown_count": unknown_cells + len(unknown_cost),
            # 운영 부담 값이 하나도 없으면 보수적으로 high
            "ops_burden": max(ops, key=OPS_ORDER.get) if ops else "high",
            "human_steps": 0,
            "change_effort": effort,
            "is_current": is_current,
            "paths": [], "cross_scope_violations": [], "sizing": [],
            "transforms": list(combo.transforms),
            "external_scopes": sorted(combo.external),
            # 판정이 기댄 유도 값(정의상/유도). AgentCore 요약에 그대로 보여 준다
            "derived_facts": [{"scope": c["scope"], "component": c["candidate"], **p}
                              for c in combo.cells for p in c.get("derived_passes", [])],
            # 순위에만 쓰고 출력하지 않는다
            "_sort": (combo.evidence_unknown > 0, bool(unknown_cost), len(unknown_cost), total,
                      unknown_cells + len(unknown_cost), configs, combo.name),
        }

    def run(self) -> dict:
        batch = (self.profile or {}).get("batch_only") or {}
        combos = [] if batch.get("value") else self.combos()
        feasible, rejected = [], []
        for combo in combos:
            violations = combo.violations()
            if violations or combo.blocked:
                reasons = list(combo.blocked) + [
                    {"type": "violation", "detail": f"{v['rule']}: {v.get('message', '')}", "violation": v}
                    for v in violations]
                rejected.append({"id": combo.name, "reasons": reasons})
            else:
                feasible.append(self._candidate(combo))
        feasible.sort(key=lambda c: (c["_sort"][0], c["_sort"][1], c["_sort"][2], c["_sort"][3], c["_sort"][4],
                                     OPS_ORDER[c["ops_burden"]], c["_sort"][5], c["_sort"][6]))
        for i, cand in enumerate(feasible, start=1):
            cand["id"], cand["rank"] = f"C{i}", i
            del cand["_sort"]
        outcome, detail = self._outcome(feasible, batch)
        no_feasible = None
        if outcome == "no_feasible":
            no_feasible = {"blocking": [r["violation"] for rej in rejected for r in rej["reasons"]
                                        if "violation" in r],
                           "suggestions": [],
                           "enabling_transforms": []}
        transforms = []
        if any(SQLITE_TRANSFORM in c.transforms for c in combos):
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

    def _outcome(self, feasible: list[dict], batch: dict) -> tuple[str, dict | None]:
        if batch.get("value"):
            return "not_deployable", {"message": batch.get("reason") or "사람이 실행하는 도구", "current": []}
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
