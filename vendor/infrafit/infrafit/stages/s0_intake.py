"""S0 접수: 커밋 고정, 파일 범위, 언어 비율, 사용자 조정 파일."""

from __future__ import annotations

import yaml

from infrafit.repo import Snapshot, language_ratios
from infrafit.run import RunContext, code_version, input_hash, now_iso


def _overrides(snap: Snapshot) -> dict | None:
    if not snap.exists("infrafit.yaml"):
        return None
    content = snap.read("infrafit.yaml").strip()
    if not content:
        return None
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"infrafit.yaml is not valid YAML: {e}")
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"infrafit.yaml must be a mapping, got {type(data).__name__}")
    return data


def run_s0(ctx: RunContext, snap: Snapshot) -> dict:
    started = now_iso()
    overrides = _overrides(snap)
    body = {
        "repo": snap.repo,
        "commit": snap.commit,
        "files_total": snap.files_total,
        "files_scanned": len(snap.files),
        "excluded": snap.excluded,
        "languages": language_ratios(snap.files),
        "overrides": overrides,
    }
    return ctx.write_stage("S0", body, input_hash=input_hash("S0", snap.commit, overrides, code_version()),
                           started_at=started)
