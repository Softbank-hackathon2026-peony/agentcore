"""S4 추천: 조합을 만들고 순위를 매긴다(설계 §9, 계획 2 MVP)."""

from __future__ import annotations

from infrafit import kb
from infrafit.fit.recommend import build_recommendation
from infrafit.run import RunContext, code_version, input_hash, now_iso
from infrafit.stages.s3_fit import body_of


def run_s4(ctx: RunContext, inventory: dict, profile: dict, fit: dict,
           capabilities: list[dict] | None = None, rules: list[dict] | None = None) -> dict:
    started = now_iso()
    capabilities = list(kb.capabilities().values()) if capabilities is None else capabilities
    rules = list(kb.rules()) if rules is None else rules
    h = input_hash("S4", body_of(inventory), body_of(profile), body_of(fit), capabilities, rules,
                   code_version())
    cached = ctx.cached("S4", h)
    if cached is not None:
        return cached
    body = build_recommendation(inventory, profile, fit, capabilities, rules)
    return ctx.write_stage("S4", body, input_hash=h, started_at=started)
