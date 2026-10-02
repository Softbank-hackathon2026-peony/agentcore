"""analyze 모드 (그림 05~08): 분석·추천 + 빌드 파일 생성.

흐름: 소스 읽기 → 스캔(코드) → LLM 판단 → 검사·보정(코드) → 비용·권한(코드) → 빌드 파일 → 저장
"""
import re
import secrets
from datetime import datetime, timezone

from . import buildfiles, catalog, config, cost, source
from .errors import AgentError
from .scan import scan as run_scan
from .schemas import LLMRecommendation
from .storage import Store

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRETISH = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|DSN|DATABASE_URL)", re.I)
# 이름이 평범해도 값이 비밀값처럼 생기면 env 에 넣지 않는다 (README·주석에서 흘러든 키 차단)
SECRET_VALUE = re.compile(r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|\bsk-[A-Za-z0-9_\-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}"
                          r"|\bxox[abposr]-|-----BEGIN [A-Z ]*PRIVATE KEY|\bAIza[0-9A-Za-z_\-]{30,}")
RESERVED_ENV = {"PORT", "AWS_LWA_PORT", "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}


def new_analysis_id() -> str:
    return f"ana-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{secrets.token_hex(2)}"


def run(payload: dict, brain, store: Store) -> dict:
    project_id = _need(payload, "project_id")
    source_uri = _need(payload, "source_uri")
    analysis_id = payload.get("analysis_id") or new_analysis_id()
    revision = None
    if payload.get("revision_message"):
        revision = {"message": str(payload["revision_message"])[:2000],
                    "previous": payload.get("previous_recommendation")}

    src = source.load(source_uri)
    scan = run_scan(src)
    rec, df = brain.analyze(src, scan, revision)

    recommendation, notes = validate(rec, src, scan)
    port = recommendation["container_port"]
    dockerfile, fixes = buildfiles.normalize_dockerfile(df.dockerfile, port)

    files = {buildfiles.DOCKERFILE_NAME: dockerfile, "dockerignore": buildfiles.DOCKERIGNORE,
             "buildspec.yml": buildfiles.buildspec()}
    build_prefix = f"projects/{project_id}/build/{analysis_id}/attempt-1/"
    for name, text in files.items():
        store.put_text(build_prefix + name, text)
    recommendation["dockerfile_notes"] = list(df.notes) + fixes
    recommendation.update({"project_id": project_id, "analysis_id": analysis_id,
                           "commit_sha": payload.get("commit_sha"), "model_id": config.MODEL_ID,
                           "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    rec_uri = store.put_json(f"projects/{project_id}/analysis/{analysis_id}/recommendation.json", recommendation)

    return {
        "status": "ok", "mode": "analyze", "project_id": project_id, "analysis_id": analysis_id,
        "recommendation": recommendation, "recommendation_uri": rec_uri,
        "build_files": {"attempt": 1, "uri_prefix": store.prefix_uri(build_prefix),
                        "dockerfile": dockerfile, "buildspec": files["buildspec.yml"]},
        "validation_notes": notes,
    }


def validate(rec: LLMRecommendation, src: source.SourceTree, scan: dict) -> tuple[dict, list[str]]:
    """LLM 출력을 허용 목록·실제 파일과 대조해서 고친다. 고친 내용은 notes로 남긴다."""
    notes: list[str] = []
    warnings = list(dict.fromkeys([*scan["warnings"], *rec.warnings]))

    target = rec.target
    if not catalog.is_deployable(target):
        ranked = sorted(rec.candidates, key=lambda c: -c.fit)
        fallback = next((c.target for c in ranked if catalog.is_deployable(c.target)), "aws_ec2")
        notes.append(f"추천 대상 {target}은 지금 배포 불가 → {fallback}로 변경")
        target = fallback

    health = rec.health_path if rec.health_path.startswith("/") else "/" + rec.health_path
    if len(health) > 200 or any(ch in health for ch in " \n\t"):
        notes.append("health_path 형식 오류 → '/'")
        health = "/"

    env, secrets_needed = {}, list(rec.required_secrets)
    for k, v in list(rec.env.items())[:30]:
        if not ENV_KEY_RE.match(k) or k in RESERVED_ENV:
            notes.append(f"환경변수 제외: {k}")
            continue
        if SECRETISH.search(k):
            secrets_needed.append(k)
            continue
        if SECRET_VALUE.search(str(v)):
            notes.append(f"환경변수 값이 비밀값처럼 보여 env 에서 빼고 직접 넣도록 바꿈: {k}")
            secrets_needed.append(k)
            continue
        env[k] = str(v)[:200].replace("\n", " ")

    clues = []
    for c in rec.clues[:8]:
        p = source.normalize(c.file)
        if not p or not src.exists(p):
            notes.append(f"존재하지 않는 파일을 근거로 들어서 제외: {c.file}")
            continue
        line = c.line
        if line is not None and not (1 <= line <= len(src.lines(p))):
            notes.append(f"근거 줄 번호가 범위를 벗어나서 지움: {p}:{line}")
            line = None
        clues.append({**c.model_dump(), "file": p, "line": line})

    seen, candidates = set(), []
    ordered = sorted(rec.candidates, key=lambda c: (c.target != target, -c.fit))
    for c in ordered:
        if c.target in seen:
            continue
        seen.add(c.target)
        candidates.append(_candidate(c.target, c.fit, c.verdict, c.why, rec.size))
    for tid in catalog.TARGETS:        # LLM이 빠뜨린 대상도 표시 (적합도 없음)
        if tid not in seen:
            candidates.append(_candidate(tid, None, "부적합" if not catalog.is_deployable(tid) else "적합",
                                         "모델이 평가하지 않음", rec.size))
    for i, c in enumerate(candidates, 1):
        c["rank"] = i

    t = catalog.TARGETS[target]
    supported = rec.supported and not scan["compose_services"][1:] and not scan["k8s_dirs"]
    return {
        # ↓ Terraform Worker가 읽는 필드
        "cloud": t["cloud"], "architecture": t["architecture"], "container_port": rec.container_port,
        "size": rec.size, "health_path": health, "env": env, "reason": rec.reason,
        # ↓ 화면·사용자 검토용
        "target": target, "label": t["label"], "summary": rec.summary,
        "required_secrets": sorted(set(secrets_needed)), "permissions": t["permissions"],
        "cost": cost.estimate(target, rec.size), "clues": clues, "candidates": candidates,
        "supported": supported, "warnings": warnings,
    }, notes


def _candidate(tid: str, fit, verdict, why, size) -> dict:
    t = catalog.TARGETS[tid]
    return {"target": tid, "cloud": t["cloud"], "architecture": t["architecture"], "label": t["label"],
            "deployable": catalog.is_deployable(tid), "fit": fit, "verdict": verdict, "why": why,
            "size_spec": t["sizes"][size], "permissions": t["permissions"], "cost": cost.estimate(tid, size)}


def _need(payload: dict, key: str):
    v = payload.get(key)
    if v in (None, ""):
        raise AgentError("bad_request", f"필수 값이 없습니다: {key}")
    return v
