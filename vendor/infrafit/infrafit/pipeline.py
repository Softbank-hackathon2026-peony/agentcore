"""단계 실행 순서."""

from __future__ import annotations

from pathlib import Path

from infrafit.consistency import ConsistencyError, check_s1, check_s2, check_s3, check_s4
from infrafit.repo import open_snapshot
from infrafit.run import RunContext
from infrafit.stages.s0_intake import run_s0
from infrafit.stages.s1_inventory import run_s1
from infrafit.stages.s2_profile import run_s2
from infrafit.stages.s3_fit import run_s3
from infrafit.stages.s4_recommend import run_s4

ORDER = ["S0", "S1", "S2", "S3", "S4"]


def analyze(source: str, out_root: Path, until: str = "S1", run_id: str | None = None) -> RunContext:
    if until not in ORDER:
        raise ValueError(f"지원하지 않는 단계: {until}")
    ctx = RunContext.create(out_root, run_id)
    snap = open_snapshot(source, ctx.out_dir, exclude=out_root)
    run_s0(ctx, snap)
    if until == "S0":
        return ctx
    inventory = run_s1(ctx, snap)
    issues = check_s1(inventory, snap.root)
    if issues:
        raise ConsistencyError(issues)
    if until == "S1":
        return ctx
    profile = run_s2(ctx, snap, inventory)
    issues = check_s2(profile, inventory, snap.root)
    if issues:
        raise ConsistencyError(issues)
    if until == "S2":
        return ctx
    fit = run_s3(ctx, inventory, profile)
    issues = check_s3(fit, inventory, profile)
    if issues:
        raise ConsistencyError(issues)
    if until == "S3":
        return ctx
    recommendation = run_s4(ctx, inventory, profile, fit)
    issues = check_s4(recommendation, fit, inventory, profile)
    if issues:
        raise ConsistencyError(issues)
    return ctx
