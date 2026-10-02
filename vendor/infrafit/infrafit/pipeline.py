"""단계 실행 순서."""

from __future__ import annotations

from pathlib import Path

from infrafit.consistency import ConsistencyError, check_s1
from infrafit.repo import open_snapshot
from infrafit.run import RunContext
from infrafit.stages.s0_intake import run_s0
from infrafit.stages.s1_inventory import run_s1

ORDER = ["S0", "S1"]


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
    return ctx
