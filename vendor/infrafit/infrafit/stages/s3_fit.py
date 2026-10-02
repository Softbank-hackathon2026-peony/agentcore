"""S3 적합성: 범위 × 후보에 규칙을 적용한다(설계 §8)."""

from __future__ import annotations

from infrafit import kb
from infrafit.fit.matrix import build_fit
from infrafit.run import RunContext, code_version, input_hash, now_iso


def body_of(stage_output: dict) -> dict:
    """캐시 키용: 실행마다 바뀌는 meta를 뺀 본문."""
    return {k: v for k, v in stage_output.items() if k != "meta"}


def run_s3(ctx: RunContext, inventory: dict, profile: dict,
           capabilities: list[dict] | None = None, rules: list[dict] | None = None) -> dict:
    started = now_iso()
    capabilities = list(kb.capabilities().values()) if capabilities is None else capabilities
    rules = list(kb.rules()) if rules is None else rules
    h = input_hash("S3", body_of(inventory), body_of(profile), capabilities, rules, code_version())
    cached = ctx.cached("S3", h)
    if cached is not None:
        return cached
    body = build_fit(inventory, profile, capabilities, rules)
    return ctx.write_stage("S3", body, input_hash=h, started_at=started)
