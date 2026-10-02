"""단계 간 일관성 검사(설계 §15.1). 이 계획에서는 S0~S1."""

from __future__ import annotations

import json
from pathlib import Path

from infrafit import kb
from infrafit.detect.environments import env_slug
from infrafit.schema import SchemaError, validate

SPECIAL_COMPONENTS = {"unmapped", "pending"}


class ConsistencyError(RuntimeError):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


def _evidence_items(obj):
    if isinstance(obj, dict):
        if {"path", "snippet"} <= obj.keys() and "line" in obj:
            yield obj
        for value in obj.values():
            yield from _evidence_items(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _evidence_items(value)


def check_s1(inventory: dict, repo_root: Path | None) -> list[str]:
    issues: list[str] = []
    workloads = {w["id"] for w in inventory["workloads"]}
    ids = ([w["id"] for w in inventory["workloads"]] + [e["id"] for e in inventory["endpoints"]]
           + [d["id"] for d in inventory["datastores"]] + [p["id"] for p in inventory["request_paths"]])
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        issues.append(f"duplicate scope id: {dup}")
    scopes = set(ids)
    catalog = kb.catalog()
    seen_endpoints: set[tuple] = set()
    for e in inventory["endpoints"]:
        if e["workload"] not in workloads:
            issues.append(f"endpoint {e['id']}: unknown workload {e['workload']}")
        key = (e["workload"], e["method"], e["route"], e["handler"]["path"], e["handler"]["line"])
        if key in seen_endpoints:
            issues.append(f"endpoint {e['id']}: duplicate of another endpoint in {e['workload']}")
        seen_endpoints.add(key)
    for d in inventory["datastores"]:
        for u in d["used_by"]:
            if u not in workloads:
                issues.append(f"datastore {d['id']}: unknown workload {u}")
    for c in inventory["current_components"]:
        if c["scope"] not in scopes:
            issues.append(f"current component: unknown scope {c['scope']}")
        if c["component"] not in SPECIAL_COMPONENTS and c["component"] not in catalog:
            issues.append(f"current component {c['scope']}: not in catalog {c['component']}")
    env_list = [e["name"] for e in inventory.get("environments", [])]
    env_names = set(env_list)
    for dup in sorted({n for n in env_list if env_list.count(n) > 1}):
        issues.append(f"duplicate environment name: {dup}")
    for e in inventory.get("environments", []):
        for m in e.get("members", []):
            if m not in workloads:
                issues.append(f"environment {e['name']}: unknown member workload {m}")
    for c in inventory["current_components"]:
        if "environment" in c and c["environment"] not in env_names:
            issues.append(f"current component {c['scope']}: unknown environment {c['environment']}")
    ext_ids = [s["id"] for s in inventory.get("external_services", [])]
    for dup in sorted({i for i in ext_ids if ext_ids.count(i) > 1}):
        issues.append(f"duplicate external service id: {dup}")
    for s in inventory.get("external_services", []):
        for u in s["used_by"]:
            if u not in workloads:
                issues.append(f"external service {s['id']}: unknown workload {u}")
    for e in inventory["endpoints"]:
        seen_env: set = set()
        for x in e.get("exposure", []):
            if x["environment"] is not None and x["environment"] not in env_names:
                issues.append(f"endpoint {e['id']}: exposure unknown environment {x['environment']}")
            if x["environment"] in seen_env:
                issues.append(f"endpoint {e['id']}: duplicate exposure environment {x['environment']}")
            seen_env.add(x["environment"])
    for p in inventory["request_paths"]:
        if p["workload"] not in workloads:
            issues.append(f"request path {p['id']}: unknown workload {p['workload']}")
        env = p.get("environment")
        expected = "path-" + p["workload"][2:] + (f".{env_slug(env)}" if env is not None else "")
        if p["id"] != expected:
            issues.append(f"request path id {p['id']} does not match workload {p['workload']}")
        if env is not None and env not in env_names:
            issues.append(f"request path {p['id']}: unknown environment {env}")
        if [h["order"] for h in p["hops"]] != list(range(len(p["hops"]))):
            issues.append(f"request path {p['id']}: hop order is not 0..n-1")
        for h in p["hops"]:
            if h["component"] not in SPECIAL_COMPONENTS and h["component"] not in catalog:
                issues.append(f"request path {p['id']}: not in catalog {h['component']}")
    if repo_root is not None:
        for ev in _evidence_items(inventory):
            target = repo_root / ev["path"]
            if not target.is_file():
                issues.append(f"evidence file missing: {ev['path']}")
            elif isinstance(ev["line"], int):
                count = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
                if ev["line"] > count:
                    issues.append(f"evidence line out of range: {ev['path']}:{ev['line']}")
    return sorted(set(issues))


def check_run(run_dir: Path) -> list[str]:
    issues: list[str] = []
    intake = json.loads((run_dir / "intake.json").read_text(encoding="utf-8"))
    try:
        validate("Intake", intake)
    except SchemaError as e:
        issues.append(str(e))
    inventory_file = run_dir / "inventory.json"
    if inventory_file.exists():
        inventory = json.loads(inventory_file.read_text(encoding="utf-8"))
        try:
            validate("Inventory", inventory)
        except SchemaError as e:
            issues.append(str(e))
        else:
            root = Path(intake["repo"])
            issues += check_s1(inventory, root if root.is_dir() else None)
    return issues
