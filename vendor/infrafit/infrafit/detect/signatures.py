"""knowledge/signatures/*.yaml의 시그니처를 저장소에 맞춰 본다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from infrafit.detect.manifests import Manifests
from infrafit.detect.testpaths import is_test_path
from infrafit.evidence import evidence, line_at
from infrafit.repo import Snapshot

MAX_CODE_EVIDENCE = 5
# 보조 코드(스크립트·QA·도구·예제·벤치마크) 디렉터리: 근거 목록에서 다른 파일 뒤에 둔다
AUX_DIRS = frozenset({"scripts", "qa", "tools", "examples", "bench"})


def is_aux_path(rel: str) -> bool:
    """보조 코드(스크립트·QA·도구·예제·벤치마크) 디렉터리 아래 파일인가."""
    return bool(set(PurePosixPath(rel).parts[:-1]) & AUX_DIRS)


@dataclass(frozen=True)
class Match:
    signature: str
    component: str
    role: str
    status: str
    evidence: tuple[dict, ...]


def _eval(cond: dict, snap: Snapshot, manifests: Manifests) -> list[dict]:
    """맞으면 근거 목록, 안 맞으면 빈 목록."""
    if "any" in cond:
        out: list[dict] = []
        for child in cond["any"]:
            out += _eval(child, snap, manifests)
        return out
    if "all" in cond:
        parts = [_eval(child, snap, manifests) for child in cond["all"]]
        return [e for part in parts for e in part] if all(parts) else []
    if "dependency" in cond:
        return [evidence(snap, rel, line, "tech")
                for rel, line in manifests.locations.get(cond["dependency"].lower(), [])]
    if "code" in cond:
        code = cond["code"]
        flags = re.MULTILINE | (re.IGNORECASE if code.get("flags") == "i" else 0)
        rx = re.compile(code["regex"], flags)
        globs = code["glob"] if isinstance(code["glob"], list) else [code["glob"]]
        files = {rel for g in globs for rel in snap.glob(g)}
        out = []
        for rel in sorted(files, key=lambda r: (is_aux_path(r), r)):
            if is_test_path(rel):  # 테스트 코드의 흔적은 실제 배포 구성의 근거가 아니다
                continue
            if code.get("multiline"):  # 줄을 넘는 식(메서드 체인 등): 파일 전체에서 찾고 줄은 일치 시작 위치
                text = snap.read(rel)
                lines = sorted({line_at(text, m.start()) for m in rx.finditer(text)})
            else:
                lines = [i for i, text in enumerate(snap.lines(rel), 1) if rx.search(text)]
            for i in lines:
                out.append(evidence(snap, rel, i, "tech"))
                if len(out) >= MAX_CODE_EVIDENCE:
                    return out
        return out
    raise ValueError(f"알 수 없는 조건: {cond}")


def match_signatures(snap: Snapshot, manifests: Manifests, sigs) -> list[Match]:
    out: list[Match] = []
    for sig in sigs:
        ev = _eval(sig["when"], snap, manifests)
        if not ev:
            continue
        component, status = sig["component"], sig["status"]
        for refine in sig.get("refine", []):
            extra = _eval(refine["when"], snap, manifests)
            if extra:
                component = refine["component"]
                status = refine.get("status", status)
                ev = ev + extra
                break
        # 조건 여러 개가 같은 줄을 가리킬 수 있다(같은 근거는 하나만), 보조 코드 근거는 뒤로
        unique = [e for i, e in enumerate(ev) if e not in ev[:i]]
        unique.sort(key=lambda e: is_aux_path(e["path"]))
        out.append(Match(sig["id"], component, sig["role"], status, tuple(unique)))
    return out


def unmapped_signature_labels(snap: Snapshot, manifests: Manifests, sigs) -> list[dict]:
    """구성 요소 ID가 없는 시그니처(kb.unmapped_signatures)가 맞으면 unmapped 항목 {label, evidence}. 근거가 모두
    보조 코드이면 내지 않는다."""
    out: list[dict] = []
    for sig in sigs:
        ev = _eval(sig["when"], snap, manifests)
        unique = [{k: v for k, v in e.items() if k != "kind"} for i, e in enumerate(ev) if e not in ev[:i]]
        if unique and not all(is_aux_path(e["path"]) for e in unique):
            unique.sort(key=lambda e: is_aux_path(e["path"]))
            out.append({"label": sig["label"], "evidence": unique})
    return out
