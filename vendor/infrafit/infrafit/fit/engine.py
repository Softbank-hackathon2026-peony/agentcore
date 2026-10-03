"""규칙 엔진(설계 §8): 범위 × 후보마다 규칙을 적용해 FitCell을 만든다.

형식은 계획 2의 `고정 형식`을 따른다.
- 능력: `components[].capabilities[KEY] = {value, source: {doc, line, url, quote}}`
- 규칙: `{id, when: {dimension, equals|...}, require: {capability, equals|...}, otherwise: infeasible|config,
  config_from, message}`

판정 순서: 위반이 하나라도 있으면 infeasible, 아니면 모르는 키가 있으면 unknown,
아니면 설정 요구가 있으면 feasible_with_config, 아니면 feasible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COMPUTE_FAMILIES = ("cp",)
DATA_FAMILIES = ("ds", "ca")
MISSING = object()

ENGINE_HINTS = (("sqlite", "sqlite"), ("postgres", "postgres"), ("redis", "redis"),
                ("elasticache", "redis"), ("memorystore", "redis"), ("valkey", "redis"))


# ---------- 능력 값 ----------

def cap_entry(component: dict | None, key: str):
    """능력 항목 {value, source}를 돌려준다. 키가 없거나 값이 null/unknown이면 MISSING."""
    if component is None:
        return MISSING
    entry = (component.get("capabilities") or {}).get(key, MISSING)
    if entry is MISSING:
        return MISSING
    if not isinstance(entry, dict) or "value" not in entry:
        entry = {"value": entry}
    if entry["value"] is None or entry["value"] == "unknown":
        return MISSING
    return entry


def cap_value(component: dict | None, key: str, default=None):
    entry = cap_entry(component, key)
    return default if entry is MISSING else entry["value"]


def source_of(entry) -> dict:
    """능력 출처(doc, line, url, quote)를 스키마 Source({ref, quote})로 옮긴다."""
    src = (entry.get("source") if isinstance(entry, dict) else None) or {}
    doc_ref = ""
    if src.get("doc"):
        doc_ref = f"docs/research/{src['doc']}" + (f"#L{src['line']}" if src.get("line") else "")
    out = {"ref": src.get("url") or doc_ref or "knowledge/capabilities.yaml"}
    if src.get("quote"):
        out["quote"] = str(src["quote"])
    if src.get("checked_at"):
        out["checked_at"] = str(src["checked_at"])
    return out


def doc_location(entry) -> str | None:
    src = (entry.get("source") if isinstance(entry, dict) else None) or {}
    if src.get("doc"):
        return f"docs/research/{src['doc']}" + (f":{src['line']}" if src.get("line") else "")
    return None


def engine_of(component_id: str, components: dict[str, dict]) -> str | None:
    """데이터 구성 요소의 엔진(postgres|redis|sqlite). 능력 표 DS.engine이 먼저, 없으면 ID의 제품 이름."""
    value = cap_value(components.get(component_id), "DS.engine")
    if value is not None:
        return str(value)
    product = component_id.split(":", 1)[-1].split("/")[1] if "/" in component_id else ""
    for hint, engine in ENGINE_HINTS:
        if hint in product:
            return engine
    return None


# ---------- 조건 ----------

def _cmp(actual, cond: dict) -> bool:
    """`cond`의 연산자 하나(equals, not_equals, in, gte, lte, exists)로 값을 비교한다."""
    seq = actual if isinstance(actual, list) else [actual]
    if "equals" in cond:
        return any(v == cond["equals"] for v in seq)
    if "not_equals" in cond:
        return all(v != cond["not_equals"] for v in seq)
    if "in" in cond:
        return any(v in cond["in"] for v in seq)
    if "gte" in cond or "lte" in cond:
        nums = [v for v in seq if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not nums:
            return False
        if "gte" in cond and not all(v >= cond["gte"] for v in nums):
            return False
        if "lte" in cond and not all(v <= cond["lte"] for v in nums):
            return False
        return True
    if "exists" in cond:
        present = actual not in (None, False, [], "", "none", "없음")
        return present == bool(cond["exists"])
    return actual not in (None, False, [], "", "none", "없음")


def required_spec(cond: dict | None):
    if not cond:
        return None
    return {k: v for k, v in cond.items() if k not in ("capability", "dimension")}


def match_when(when: dict, dims: list[dict]) -> list[dict]:
    """when 조건에 맞는 차원 값 목록. 비어 있으면 규칙이 적용되지 않는다."""
    if "any" in when:
        out: list[dict] = []
        for child in when["any"]:
            out += match_when(child, dims)
        return out
    if "all" in when:
        out = []
        for child in when["all"]:
            hit = match_when(child, dims)
            if not hit:
                return []
            out += hit
        return out
    return [d for d in dims if d["dimension"] == when.get("dimension") and _cmp(dim_value(d.get("value")), when)]


def dim_value(value):
    """S2 차원 값을 비교할 값으로: {value, kinds} 객체(B1·B2·E2)는 value, 목록(A1)·문자열은 그대로."""
    if isinstance(value, dict):
        return value.get("value")
    return value


def check_require(require: dict | None, component: dict | None):
    """요구 조건 평가 → ("pass"|"fail"|"unknown", 실패/모름에 관련된 (key, entry) 목록)."""
    if require is None:
        return "fail", []
    if "any" in require or "all" in require:
        children = [check_require(c, component) for c in require.get("any") or require.get("all")]
        states = [s for s, _ in children]
        refs = [r for _, rs in children for r in rs]
        if "any" in require:
            if "pass" in states:
                return "pass", []
            return ("unknown" if "unknown" in states else "fail"), refs
        if "fail" in states:
            return "fail", [r for (s, rs) in children if s == "fail" for r in rs]
        return ("unknown" if "unknown" in states else "pass"), refs if "unknown" in states else []
    key = require["capability"]
    entry = cap_entry(component, key)
    if entry is MISSING:
        return "unknown", [(key, None)]
    return ("pass" if _cmp(entry["value"], require) else "fail"), [(key, entry)]


def rule_keys(rule: dict) -> list[str]:
    keys: list[str] = []

    def walk(cond):
        if not cond:
            return
        if "any" in cond or "all" in cond:
            for c in cond.get("any") or cond.get("all"):
                walk(c)
        elif "capability" in cond:
            keys.append(cond["capability"])
    walk(rule.get("require"))
    if rule.get("config_from"):
        keys.append(rule["config_from"])
    return keys


def rule_target(rule: dict) -> str:
    """규칙이 비교하는 계열: CP.* 키만 쓰면 compute, 아니면 data."""
    keys = rule_keys(rule)
    return "compute" if keys and all(k.startswith("CP.") for k in keys) else ("data" if keys else "compute")


# ---------- 셀 ----------

def _evidence_text(dims: list[dict]) -> str:
    parts = []
    for d in dims:
        ev = (d.get("evidence") or [])[:2]
        where = ", ".join(f"{e['path']}:{e.get('line')}" for e in ev) or d.get("source", "")
        parts.append(f"{d['dimension']}={d.get('value')!r} ({where})")
    return "; ".join(parts)


@dataclass
class Cell:
    scope: str
    candidate: str
    violations: list[dict] = field(default_factory=list)
    requires_config: list[dict] = field(default_factory=list)
    unknown_keys: list[str] = field(default_factory=list)
    is_current: bool = False

    @property
    def result(self) -> str:
        if self.violations:
            return "infeasible"
        if self.unknown_keys:
            return "unknown"
        if self.requires_config:
            return "feasible_with_config"
        return "feasible"

    def to_dict(self) -> dict:
        return {"scope": self.scope, "candidate": self.candidate, "result": self.result,
                "violations": self.violations, "requires_config": self.requires_config,
                "unknown_keys": sorted(set(self.unknown_keys)), "is_current": self.is_current}


def _violation(rule: dict, dims: list[dict], key: str, entry, required) -> dict:
    dim_ids = sorted({d["dimension"] for d in dims})
    message = rule.get("message") or rule["id"]
    loc = doc_location(entry) if entry is not None else None
    message += f" — 요구: {_evidence_text(dims)}"
    if loc:
        message += f" — 능력 근거: {loc}"
    return {"rule": rule["id"], "dimension": dim_ids[0] if len(dim_ids) == 1 else dim_ids,
            "required": required, "capability_key": key,
            "actual": None if entry is None else entry["value"],
            "source": source_of(entry) if entry is not None else {"ref": "knowledge/rules.yaml#" + rule["id"]},
            "message": message}


def evaluate(scope: str, kind: str, component_id: str, component: dict | None,
             dims: list[dict], rules: list[dict], is_current: bool = False) -> Cell:
    """한 (범위, 후보) 쌍에 규칙을 모두 적용한다. kind는 compute|data."""
    cell = Cell(scope=scope, candidate=component_id, is_current=is_current)
    for rule in sorted(rules, key=lambda r: r["id"]):
        if rule_target(rule) != kind:
            continue
        hit = match_when(rule.get("when") or {}, dims)
        if not hit:
            continue
        state, refs = check_require(rule.get("require"), component)
        if state == "pass":
            continue
        if state == "unknown":
            cell.unknown_keys += [k for k, e in refs if e is None]
            continue
        required = required_spec(rule.get("require"))
        if rule.get("otherwise") == "config" and rule.get("config_from"):
            setting = cap_entry(component, rule["config_from"])
            if setting is not MISSING and setting["value"] not in (False, ""):
                req = {"rule": rule["id"], "setting": str(setting["value"]),
                       "value": True,
                       "why": (rule.get("message") or rule["id"]) + f" — 요구: {_evidence_text(hit)}"}
                if setting.get("source"):
                    req["why"] += f" — 설정 근거: {doc_location(setting) or source_of(setting)['ref']}"
                cell.requires_config.append(req)
                continue
            # 설정 이름이 없다 = 켤 방법이 없다(형식: 없으면 키 생략) → 위반
            key, entry = (refs[0] if refs else (rule["config_from"], None))
            cell.violations.append(_violation(rule, hit, key, entry, required))
            continue
        key, entry = refs[0] if refs else ((rule_keys(rule) or ["-"])[0], None)
        cell.violations.append(_violation(rule, hit, key, entry, required))
    return cell
