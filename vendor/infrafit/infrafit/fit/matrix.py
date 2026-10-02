"""S3 출력 본문: 범위 × 후보 적합성 행렬과 현재 구성 판정."""

from __future__ import annotations

from infrafit.fit.engine import evaluate
from infrafit.fit.scopes import app_scopes, candidates_for, data_scopes

VERDICT = {"feasible": "keep", "feasible_with_config": "modify", "infeasible": "replace"}


def components_by_id(capabilities: list[dict]) -> dict[str, dict]:
    return {c["id"]: c for c in capabilities}


def all_scopes(inventory: dict, profile: dict, components: dict[str, dict]):
    return app_scopes(inventory, profile) + data_scopes(inventory, profile, components)


def build_fit(inventory: dict, profile: dict, capabilities: list[dict], rules: list[dict]) -> dict:
    components = components_by_id(capabilities)
    matrix: list[dict] = []
    assessment: list[dict] = []
    for scope in all_scopes(inventory, profile, components):
        for cid in candidates_for(scope, components):
            cell = evaluate(scope.id, scope.kind, cid, components.get(cid), scope.dims, rules,
                            is_current=cid in scope.current)
            matrix.append(cell.to_dict())
            if cell.is_current and cell.result in VERDICT:
                assessment.append({"scope": scope.id, "component": cid, "verdict": VERDICT[cell.result],
                                   "reasons": cell.violations})
    matrix.sort(key=lambda c: (c["scope"], c["candidate"]))
    assessment.sort(key=lambda a: (a["scope"], a["component"]))
    # 위생 규칙·경로 검사는 이 MVP에서 계산하지 않는다(빈 목록)
    return {"matrix": matrix, "current_assessment": assessment, "hygiene": [], "path_checks": []}
