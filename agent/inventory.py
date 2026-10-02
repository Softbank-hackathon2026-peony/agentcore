"""InfraFit S1~S4 인벤토리·추천 (LLM 없음, 규칙 기반).

vendor/infrafit 의 InfraFit을 별도 프로세스로 S0~S4까지 돌려서 inventory.json (S1),
profile.json (S2), fit.json (S3), recommendation.json (S4) 을 읽고,
LLM 프롬프트에 넣을 수 있게 작게(약 10KB 이하) 요약한다.

- S1 은 성공했는데 S2~S4 에서 실패·시간 초과하면 S1 요약은 그대로 두고
  `stage_error: {"stage", "message"}` 로 알린다 (status 는 "ok").

- 소스는 임시 폴더에 풀어서 넘긴다. 비밀 파일(.env, 키 등)은 쓰지 않는다.
  .env.example 류는 변수 이름만 남기고 값은 지운 채로 쓴다 (줄 번호는 그대로).
- 별도 프로세스 + 시간 제한: InfraFit이 멈추거나 죽어도 analyze 는 계속된다.
- 절대 예외를 던지지 않는다. 실패하면 {"status": "error"|"timeout", "message": ...}.
- 요약의 file:line 은 inventory.json 의 근거를 그대로 옮긴 것이다 (실제 저장소 경로).
"""
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from . import catalog
from .source import SourceTree, is_secret

CODE_ROOT = Path(__file__).resolve().parent.parent        # 배포 zip 루트 (의존성도 여기 설치됨)
VENDOR_DIR = CODE_ROOT / "vendor" / "infrafit"
MAX_SUMMARY_BYTES = 10000                                  # 요약 전체 (S1 + recommendation)
MAX_RECOMMENDATION_BYTES = 4000                            # 그중 recommendation 블록
APP_DIMENSIONS = ("A1", "A2", "A3", "A4", "B1", "B2", "B3", "E2")
ENV_EXAMPLE_FILES = {".env.example", ".env.sample", ".env.template", "env.example"}
EXPLICIT_SETTING = re.compile(r"timeout|body", re.I)
RUN_ID = "run"

# InfraFit pipeline.analyze 를 그대로 쓰고, 단계 함수가 불릴 때마다 진행 단계를 파일에 남긴다.
# 단계 N 의 정합성 검사는 단계 N+1 함수가 불리기 전에 끝나므로, 실패 단계 = 마지막으로 시작한 단계다.
_RUNNER = """
import json, sys, traceback
from pathlib import Path
from infrafit import pipeline

out, progress = Path(sys.argv[2]), Path(sys.argv[2]) / "progress.json"
out.mkdir(parents=True, exist_ok=True)
state = {"stage": "S0"}

def _track(stage, fn):
    def run(*a, **k):
        state["stage"] = stage
        progress.write_text(json.dumps(state), encoding="utf-8")
        return fn(*a, **k)
    return run

for st in ("S1", "S2", "S3", "S4"):
    name = "run_" + st.lower()
    setattr(pipeline, name, _track(st, getattr(pipeline, name)))
try:
    pipeline.analyze(sys.argv[1], out, until="S4", run_id=sys.argv[3])
except Exception as e:
    state["error"] = (type(e).__name__ + ": " + str(e))[:300]
    progress.write_text(json.dumps(state), encoding="utf-8")
    traceback.print_exc()
    sys.exit(3)
state["stage"] = "done"
progress.write_text(json.dumps(state), encoding="utf-8")
"""
STAGE_FILES = {"S2": "profile.json", "S3": "fit.json", "S4": "recommendation.json"}


def run_inventory(src: SourceTree, timeout_s: int = 60) -> dict:
    try:
        with tempfile.TemporaryDirectory(prefix="pawploy-inv-") as tmp:
            repo, out = Path(tmp) / "repo", Path(tmp) / "out"
            _materialize(src, repo)
            env = {**os.environ, "PYTHONPATH": os.pathsep.join(
                [str(VENDOR_DIR), str(CODE_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])])}
            stage_error = None
            try:
                proc = subprocess.run([sys.executable, "-c", _RUNNER, str(repo), str(out), RUN_ID],
                                      capture_output=True, text=True, timeout=timeout_s, env=env, cwd=tmp)
            except subprocess.TimeoutExpired:
                stage = _progress(out).get("stage")
                if stage not in STAGE_FILES:
                    return {"status": "timeout", "message": f"InfraFit 인벤토리가 {timeout_s}초 안에 끝나지 않음"}
                stage_error = {"stage": stage, "message": f"InfraFit {stage} 가 {timeout_s}초 안에 끝나지 않음 (timeout)"}
            else:
                if proc.returncode != 0:
                    prog = _progress(out)
                    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["(출력 없음)"]
                    message = f"InfraFit 실패 (exit {proc.returncode}): {(prog.get('error') or tail[0])[:300]}"
                    if prog.get("stage") not in STAGE_FILES:
                        return {"status": "error", "message": message}
                    stage_error = {"stage": prog["stage"], "message": message}
            run_dir = out / RUN_ID
            inventory = json.loads((run_dir / "inventory.json").read_text(encoding="utf-8"))
            later = {}
            if stage_error is None:
                later = {st: json.loads((run_dir / f).read_text(encoding="utf-8")) for st, f in STAGE_FILES.items()}
            result = {"status": "ok", "infrafit_commit": _commit(),
                      "summary": summarize(inventory, later.get("S2"), later.get("S3"), later.get("S4"))}
            if stage_error:
                result["stage_error"] = stage_error
            return result
    except Exception as e:  # noqa: BLE001 — 인벤토리는 보조 정보라 어떤 실패도 analyze 를 막지 않는다
        return {"status": "error", "message": f"{type(e).__name__}: {str(e)[:300]}"}


def _progress(out: Path) -> dict:
    try:
        return json.loads((out / "progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _materialize(src: SourceTree, root: Path) -> None:
    root.mkdir(parents=True)
    base = root.resolve()
    for p in src.paths():
        name = posixpath.basename(p).lower()
        if name in ENV_EXAMPLE_FILES:
            data = _names_only(src._files[p])  # noqa: SLF001 — 같은 패키지 내부 접근 (scan._raw_lines 와 같음)
        elif is_secret(p):
            continue
        else:
            data = src._files[p]  # noqa: SLF001
        dest = (root / p).resolve()
        if base not in dest.parents:       # SourceTree 가 이미 막지만 한 번 더
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def _names_only(data: bytes) -> bytes:
    """KEY=value → KEY= (값 제거, 줄 수 유지)."""
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        out.append(f"{m.group(1)}=" if m else "")
    return ("\n".join(out) + "\n").encode("utf-8")


def _commit() -> str:
    try:
        for line in (VENDOR_DIR / "SOURCE").read_text(encoding="utf-8").splitlines():
            if line.startswith("commit:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


# ---------------- 요약 ----------------

def _at(ev: dict | None) -> str | None:
    if not ev or not ev.get("path"):
        return None
    return f"{ev['path']}:{ev['line']}" if ev.get("line") else ev["path"]


def _ats(evs, n: int = 2) -> list[str]:
    return [a for a in (_at(e) for e in (evs or [])[:n]) if a]


def summarize(inv: dict, profile: dict | None = None, fit: dict | None = None,
              reco: dict | None = None) -> dict:
    eps = inv.get("endpoints") or []
    per_workload: dict[str, int] = {}
    for e in eps:
        per_workload[e.get("workload", "?")] = per_workload.get(e.get("workload", "?"), 0) + 1

    def endpoint_line(e: dict) -> str:
        s = f"{e.get('method', '?')} {str(e.get('route', ''))[:100]} @{_at(e.get('handler')) or '?'}"
        exp = [f"{x.get('environment') or '-'}:{x.get('value')}" for x in e.get("exposure") or []]
        return s + (f" [{', '.join(exp[:3])}]" if exp else "")

    paths = {}
    for rp in inv.get("request_paths") or []:
        hops = sorted(rp.get("hops") or [], key=lambda h: h.get("order", 0))
        settings = []
        for h in hops:
            for st in h.get("settings") or []:
                if not st.get("defaulted") and EXPLICIT_SETTING.search(st.get("key", "")):
                    where = _at(st.get("evidence"))
                    settings.append(f"{st['key']}={str(st.get('value'))[:40]}" + (f" @{where}" if where else ""))
        paths[rp["id"]] = {"workload": rp.get("workload"), "environment": rp.get("environment"),
                           "hops": [f"{h.get('kind')} {h.get('component')}" for h in hops][:8],
                           "settings": settings[:8]}

    summary = {
        "workloads": [_workload(w) for w in inv.get("workloads") or []][:20],
        "endpoints": {"total": len(eps), "per_workload": dict(list(per_workload.items())[:20]),
                      "first": [endpoint_line(e) for e in eps[:25]]},
        "datastores": [{"id": d["id"], "role": d.get("role"), "component": _component_of(inv, d["id"]),
                        "used_by": d.get("used_by", []), "status": d.get("status"), "at": _ats(d.get("evidence"))}
                       for d in inv.get("datastores") or []][:15],
        "external_services": [{"id": x["id"], "kind": x.get("kind"), "secrets": x.get("secrets", [])[:8],
                               "used_by": x.get("used_by", []), "status": x.get("status"),
                               "at": _ats(x.get("evidence"), 1)}
                              for x in inv.get("external_services") or []][:15],
        "environments": [{"name": e.get("name"), "kind": e.get("kind"), "members": e.get("members", []),
                          "manual": e.get("manual", False), "at": _at(e.get("source"))}
                         for e in inv.get("environments") or []][:10],
        "compute": [{"scope": c.get("scope"), "component": c.get("component"), "environment": c.get("environment")}
                    for c in inv.get("current_components") or []
                    if str(c.get("component", "")).startswith("cp:")][:15],
        "request_paths": dict(list(paths.items())[:10]),
        "unmapped": [u.get("label") for u in inv.get("unmapped") or []][:20],
    }
    if reco is None or profile is None:
        return _bound(summary)
    kinds = {w["id"]: w.get("kind") for w in inv.get("workloads") or [] if w.get("id")}
    block = _bound_recommendation(recommendation_summary(profile, fit or {}, reco, kinds))
    _bound(summary, MAX_SUMMARY_BYTES - _size(block) - len(', "recommendation": '))
    summary["recommendation"] = block
    return summary


def _workload(w: dict) -> dict:
    out = {"id": w["id"], "kind": w.get("kind"), "name": w.get("name"), "status": w.get("status"),
           "at": _at(w.get("entrypoint"))}
    sc = w.get("scaling")
    if isinstance(sc, dict):                         # 저장소가 정한 레플리카·오토스케일 (HPA, compose replicas 등)
        out["scaling"] = {"min": sc.get("min"), "max": sc.get("max"), "autoscale": bool(sc.get("autoscale"))}
    return out


def _component_of(inv: dict, scope: str) -> str | None:
    return next((c.get("component") for c in inv.get("current_components") or [] if c.get("scope") == scope), None)


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _bound(summary: dict, limit: int = MAX_SUMMARY_BYTES) -> dict:
    """크기 상한을 넘으면 가장 긴 목록부터 반씩 줄인다."""
    def lists(s):
        yield "endpoints.first", s["endpoints"], "first"
        for k in ("workloads", "datastores", "external_services", "environments", "compute", "unmapped"):
            yield k, s, k
        yield "request_paths", s, "request_paths"

    while _size(summary) > limit:
        name, holder, key = max(lists(summary), key=lambda t: _size(t[1][t[2]]))
        items = holder[key]
        if not items:                      # 더 줄일 목록이 없다
            break
        if len(items) <= 1:
            holder[key] = type(items)()
        elif isinstance(items, dict):
            holder[key] = dict(list(items.items())[: len(items) // 2])
        else:
            holder[key] = items[: len(items) // 2]
        summary.setdefault("truncated", [])
        if name not in summary["truncated"]:
            summary["truncated"].append(name)
    return summary


# ---------------- 추천 (S2~S4) ----------------

@lru_cache(maxsize=1)
def _targets() -> dict[str, str]:
    """capabilities.yaml 의 구성 요소 id → AgentCore 배포 대상 id (catalog.TARGETS)."""
    try:
        import yaml
        data = yaml.safe_load((VENDOR_DIR / "knowledge" / "capabilities.yaml").read_text(encoding="utf-8"))
        return {c["id"]: c["target"] for c in data.get("components") or [] if c.get("id") and c.get("target")}
    except Exception:  # noqa: BLE001 — 대상 이름이 없어도 요약은 만든다
        return {}


def _short(text, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _app_scope(profile: dict, reco: dict) -> str | None:
    """추천 1순위 조합에서 컴퓨트(cp:)가 배정된 범위. 없으면 앱 집계 범위(aggregated_from 있는 w-*)."""
    for cand in reco.get("candidates") or []:
        for scope, comp in (cand.get("assignment") or {}).items():
            if str(comp).startswith("cp:"):
                return scope
    dims = [d for d in profile.get("dimensions") or [] if str(d.get("scope", "")).startswith("w-")]
    agg = sorted({d["scope"] for d in dims if d.get("aggregated_from")})
    scopes = agg or sorted({d["scope"] for d in dims})
    return scopes[0] if scopes else None


def _dim_value(v):
    if isinstance(v, dict) and "value" in v:          # {"value": "있음", "kinds": [...]}
        return f"{v['value']} ({', '.join(map(str, v['kinds']))})" if v.get("kinds") else v["value"]
    return v


BASIS = {"official": "공식", "derived": "유도"}


def _target_of(component: str | None) -> str | None:
    """구성 요소 id(또는 'vm-compose/cp:…/managed-data' 같은 조합 id) → AgentCore 대상 id."""
    targets = _targets()
    comp = str(component or "")
    if "cp:" in comp:
        comp = comp[comp.index("cp:"):]
    while comp:
        if comp in targets:
            return targets[comp]
        if "/" not in comp:
            return None
        comp = comp.rsplit("/", 1)[0]
    return None


def _placements(c: dict) -> list[dict]:
    """[{scope, component, target}] — S4 placement, 없으면 assignment 의 컴퓨트(cp:)에서 만든다."""
    rows = [p for p in c.get("placement") or [] if isinstance(p, dict) and p.get("scope")]
    if not rows:
        rows = [{"scope": s, "component": comp} for s, comp in sorted((c.get("assignment") or {}).items())
                if str(comp).startswith("cp:")]
    return [{"scope": p["scope"], "component": p.get("component"),
             "target": p.get("target") or _target_of(p.get("component"))} for p in rows]


def _main_placement(rows: list[dict], kinds: dict[str, str]) -> dict | None:
    """주 웹 워크로드(웹 범위 중 id 순 첫째)의 배치. 없으면 첫 배치."""
    web = sorted((r for r in rows if kinds.get(r["scope"]) == "web"), key=lambda r: r["scope"])
    return web[0] if web else (rows[0] if rows else None)


def _derived_fact(f: dict) -> str:
    src = f.get("source") or {}
    reason = src.get("reasoning") or f.get("message") or ""
    return _short(f"{f.get('scope')} {f.get('capability_key')}={json.dumps(f.get('actual'), ensure_ascii=False)}: {reason}", 160)


def _candidate(c: dict, kinds: dict[str, str] | None = None) -> dict:
    rows = _placements(c)
    main = _main_placement(rows, kinds or {})
    target = (main or {}).get("target")
    placed = {r["scope"] for r in rows}
    targets = sorted({r["target"] for r in rows if r["target"]})
    out = {"id": c.get("id"), "rank": c.get("rank"), "topology": c.get("topology"),
           "target": target, "targets": targets,
           "deployable": catalog.is_deployable(target) if target else False,
           "placement": {r["scope"]: {"component": r["component"], "target": r["target"],
                                      "deployable": catalog.is_deployable(r["target"]) if r["target"] else False}
                         for r in rows},
           "datastores": {s: comp for s, comp in sorted((c.get("assignment") or {}).items()) if s not in placed},
           "monthly_baseline_usd": (c.get("cost") or {}).get("monthly_baseline_usd"),
           "unknown_count": c.get("unknown_count", 0)}
    if len(targets) > 1:
        out["multi_target"] = True
    facts = list(dict.fromkeys(_derived_fact(f) for f in c.get("derived_facts") or [] if isinstance(f, dict)))
    if facts:
        out["derived_facts"] = facts[:3]
    if c.get("transforms"):
        out["transforms"] = c["transforms"]
    if c.get("external_scopes"):                     # 바꾸지 않고 그대로 쓰는 외부 서비스(BaaS 등)
        out["external_scopes"] = c["external_scopes"]
    return out


def recommendation_summary(profile: dict, fit: dict, reco: dict, kinds: dict[str, str] | None = None) -> dict:
    scope = _app_scope(profile, reco)
    reasons_by_assumption = {a.get("key"): a.get("reason") for a in profile.get("assumptions") or []}
    dims, dim_values, scope_values = {}, {}, {}
    for d in profile.get("dimensions") or []:
        scope_values.setdefault((d.get("scope"), d.get("dimension")), _dim_value(d.get("value")))
        if d.get("scope") != scope or d.get("dimension") not in APP_DIMENSIONS:
            continue
        row = {"value": _dim_value(d.get("value"))}
        at = _ats(d.get("evidence"), 2)
        if at:
            row["at"] = at
        if d.get("source") == "assumption":
            row["assumed"] = True
            row["why"] = _short(reasons_by_assumption.get(d.get("assumption_key") or d["dimension"]), 120)
        dims[d["dimension"]] = row
        dim_values[d["dimension"]] = row["value"]

    rejected, seen = [], set()

    def _value_for(dim, at_scope):
        """위반이 난 범위의 차원 값. 범위를 모르면 앱 범위, 그다음 그 차원이 있는 첫 범위."""
        if at_scope and (at_scope, dim) in scope_values:
            return scope_values[(at_scope, dim)]
        if dim in dim_values:
            return dim_values[dim]
        return next((v for (sc, d), v in sorted(scope_values.items(), key=lambda kv: str(kv[0]))
                     if d == dim and v is not None), None)

    def add_reason(component: str, v: dict | None, detail: str | None = None, scope: str | None = None):
        if v:
            cp = component[component.index("cp:"):] if "cp:" in component else component
            key = (cp, v.get("rule"), v.get("dimension"), v.get("capability_key"))
            if key in seen:
                return
            seen.add(key)
        entry = next((r for r in rejected if r["component"] == component), None)
        if entry is None:
            entry = {"component": component, "target": _target_of(component), "reasons": []}
            rejected.append(entry)
        if v:
            src = v.get("source") or {}
            reason = {
                "rule": v.get("rule"), "dimension": v.get("dimension"),
                "dimension_value": _value_for(v.get("dimension"), scope),
                "capability": v.get("capability_key"), "capability_value": v.get("actual"),
                "basis": BASIS.get(src.get("basis"), "공식"),
                "source": {"url": src.get("ref") or src.get("url"), "quote": _short(src.get("quote"), 140)}}
            if src.get("basis") == "derived" and src.get("reasoning"):
                reason["reasoning"] = _short(src["reasoning"], 120)
            if scope:
                reason["scope"] = scope
            entry["reasons"].append(reason)
        elif detail:
            entry["reasons"].append({"detail": _short(detail, 160)})

    for rej in reco.get("rejected") or []:
        for r in rej.get("reasons") or []:
            add_reason(rej.get("id"), r.get("violation"), r.get("detail"))
    for cell in fit.get("matrix") or []:            # S4 가 조합으로 못 만든 컴퓨트 탈락도 놓치지 않게
        if cell.get("result") == "infeasible" and str(cell.get("candidate", "")).startswith("cp:"):
            for v in cell.get("violations") or []:
                add_reason(cell["candidate"], v, scope=cell.get("scope"))
    rejected = [e for e in rejected if e["reasons"]]
    for entry in rejected:
        entry["reasons"] = entry["reasons"][:3]

    cands = reco.get("candidates") or []
    rec = next((c for c in cands if c.get("id") == reco.get("recommended")), None)
    out = {"recommended": _candidate(rec, kinds) if rec else None,
           "top": [_candidate(c, kinds) for c in cands[:5]],
           "rejected": rejected,
           "app_scope": scope, "dimensions": dims}
    if reco.get("no_feasible"):
        out["no_feasible"] = True
    if reco.get("outcome"):                           # recommended | no_feasible | static_only | not_deployable
        out["outcome"] = reco["outcome"]
        detail = reco.get("outcome_detail")
        if isinstance(detail, dict):                  # {"message": ..., "current": [{component, label, ...}]}
            current = [c.get("label") or c.get("component") for c in detail.get("current") or [] if isinstance(c, dict)]
            detail = (detail.get("message") or "") + (f" / 현재: {', '.join(map(str, current))}" if current else "")
        if detail:
            out["outcome_detail"] = str(detail)[:300]
    return out


def _bound_recommendation(block: dict) -> dict:
    """recommendation 블록 상한. 목록부터 줄인다: 하위 후보의 유도 근거 → 후보 수(3개까지) → 탈락 근거 인용
    → 후보 수(1개까지) → 탈락 수 → 차원 설명 → 1순위 유도 근거 → 배치 목록(반씩)."""
    def over() -> bool:
        return _size(block) > MAX_RECOMMENDATION_BYTES

    if over():
        for c in block["top"][1:]:
            c.pop("derived_facts", None)
    while over() and len(block["top"]) > 3:
        block["top"] = block["top"][:-1]
    if over():
        for entry in block["rejected"]:
            for r in entry["reasons"]:
                if "source" in r:
                    r["source"].pop("quote", None)
    while over() and len(block["top"]) > 1:
        block["top"] = block["top"][:-1]
    while over() and block["rejected"]:
        block["rejected"] = block["rejected"][:-1]
    for d in block["dimensions"].values():
        if not over():
            break
        d.pop("why", None)
    if over():
        for c in [block.get("recommended"), *block["top"]]:
            if c:
                c.pop("derived_facts", None)
    while over():
        cands = [c for c in [block.get("recommended"), *block["top"]] if c and len(c.get("placement") or {}) > 1]
        if not cands:
            break
        for c in cands:
            c["placement"] = dict(list(c["placement"].items())[: len(c["placement"]) // 2])
            c["placement_truncated"] = True
    return block
