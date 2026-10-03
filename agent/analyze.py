"""analyze 모드 (그림 05~08): 분석·추천 + 빌드 파일 생성.

흐름: 소스 읽기 → 스캔(코드) → LLM 판단 → 검사·보정(코드) → 비용·권한(코드) → 빌드 파일 → 저장

컨테이너가 여러 개면(InfraFit deploy_units 에 컨테이너 2개 이상 또는 데이터 저장소 컨테이너) `_run_multi`:
추천 대상은 aws_ec2_compose, 추천안에 검사·보완한 deploy_units 를 넣고, 이미지마다 빌드 파일을 만든다.
컨테이너 1개 앱의 응답은 예전과 같다.
"""
import re
import secrets
from datetime import datetime, timezone

from . import buildfiles, catalog, compose, config, cost, source, units
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
MULTI_TARGET = "aws_ec2_compose"
COMPOSE_COMPUTE = "cp:aws/ec2/docker-compose"      # InfraFit capabilities.yaml 의 같은 실행기
MAX_CANDIDATES = 5                                 # 화면 규격: 1~5순위


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
    du = units.from_scan(scan)
    if units.is_multi(du):
        return _run_multi(payload, brain, store, src, scan, rec, du, analysis_id)

    recommendation, notes = validate(rec, src, scan)
    port = recommendation["container_port"]
    dockerfile, fixes = None, []
    if no_server_evidence(scan):
        # 모델이 HTTP 래퍼를 지어내 CLI 를 공개 엔드포인트로 만들던 문제 (QA A7a). 웹 서버 근거가 하나도 없으면 배포하지 않는다
        recommendation["supported"] = False
        recommendation["warnings"].append(NO_SERVER_WARNING)
        notes.append("웹 서버 근거가 없어 supported=false, 빌드 파일을 만들지 않음")
    else:
        try:
            dockerfile, fixes = buildfiles.normalize_dockerfile(df.dockerfile, port)
        except AgentError as e:
            if e.code != "dockerfile_invalid":
                raise
            # 분석 결과까지 버리지 않는다 (README 만 있는 저장소에서 analyze 전체가 error 로 끝나던 문제, QA A7b)
            recommendation["supported"] = False
            recommendation["warnings"].append(f"Dockerfile 을 만들지 못해 배포할 수 없습니다: {e.message}")
            notes.append(f"Dockerfile 정규화 실패: {e.message}")

    build_prefix = f"projects/{project_id}/build/{analysis_id}/attempt-1/"
    files = {}
    if dockerfile is not None:
        files = {buildfiles.DOCKERFILE_NAME: dockerfile, "dockerignore": buildfiles.DOCKERIGNORE,
                 "buildspec.yml": buildfiles.buildspec()}
        for name, text in files.items():
            store.put_text(build_prefix + name, text)
    recommendation["dockerfile_notes"] = (list(df.notes) + fixes) if dockerfile is not None else []
    recommendation.update({"project_id": project_id, "analysis_id": analysis_id,
                           "commit_sha": payload.get("commit_sha"), "model_id": config.MODEL_ID,
                           "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    rec_uri = store.put_json(f"projects/{project_id}/analysis/{analysis_id}/recommendation.json", recommendation)

    return {
        "status": "ok", "mode": "analyze", "project_id": project_id, "analysis_id": analysis_id,
        "recommendation": recommendation, "recommendation_uri": rec_uri,
        "build_files": ({"attempt": 1, "uri_prefix": store.prefix_uri(build_prefix),
                         "dockerfile": dockerfile, "buildspec": files["buildspec.yml"]} if files else None),
        "validation_notes": notes,
    }


NO_SERVER_WARNING = ("HTTP 요청을 받는 서버 코드를 찾지 못했습니다 (웹 프레임워크·포트·Dockerfile·compose·HTTP 엔드포인트 없음). "
                     "Pawploy 는 웹 서비스만 배포하므로 이 저장소는 배포할 수 없습니다.")
_SERVER_KINDS = {"web", "reverse-proxy", "static-frontend"}


def no_server_evidence(scan: dict) -> bool:
    """웹 서버가 있다는 근거가 *하나도* 없으면 True. 근거가 하나라도 있으면(오탐 방지) 모델 판단을 따른다."""
    if scan.get("frameworks") or scan.get("port_hints") or scan.get("dockerfiles") \
            or scan.get("compose_services") or scan.get("static_site"):
        return False
    inv = scan.get("inventory") or {}
    if inv.get("status") != "ok":
        return False                      # InfraFit 이 없으면 판단하지 않는다
    s = inv.get("summary") or {}
    if any(w.get("kind") in _SERVER_KINDS for w in s.get("workloads") or []):
        return False
    return not (s.get("endpoints") or {}).get("total")


def validate(rec: LLMRecommendation, src: source.SourceTree, scan: dict) -> tuple[dict, list[str]]:
    """LLM 출력을 허용 목록·실제 파일과 대조해서 고친다. 고친 내용은 notes로 남긴다. (컨테이너 1개)"""
    notes: list[str] = []
    warnings = list(dict.fromkeys([*scan["warnings"], *rec.warnings]))

    target = rec.target
    if target in catalog.MULTI_CONTAINER:
        notes.append(f"추천 대상 {target}은 컨테이너 여러 개용 → 컨테이너 1개라 aws_ec2 로 변경")
        target = "aws_ec2"
    if not catalog.is_deployable(target):
        ranked = sorted(rec.candidates, key=lambda c: -c.fit)
        fallback = next((c.target for c in ranked if catalog.is_deployable(c.target)
                         and c.target not in catalog.MULTI_CONTAINER), "aws_ec2")
        notes.append(f"추천 대상 {target}은 지금 배포 불가 → {fallback}로 변경")
        target = fallback

    health = _health(rec, notes)
    env, secrets_needed = _env(rec, notes)
    clues = _clues(rec, src, notes)

    seen, candidates = set(), []
    ordered = sorted(rec.candidates, key=lambda c: (c.target != target, -c.fit))
    for c in ordered:
        if c.target in seen or c.target in catalog.MULTI_CONTAINER:
            continue
        seen.add(c.target)
        candidates.append(_candidate(c.target, c.fit, c.verdict, c.why, rec.size))
    for tid in catalog.TARGETS:        # LLM이 빠뜨린 대상도 표시 (적합도 없음)
        if tid not in seen and tid not in catalog.MULTI_CONTAINER:
            candidates.append(_candidate(tid, None, "부적합" if not catalog.is_deployable(tid) else "적합",
                                         "모델이 평가하지 않음", rec.size))
    candidates = candidates[:MAX_CANDIDATES]
    for i, c in enumerate(candidates, 1):
        c["rank"] = i

    t = catalog.TARGETS[target]
    # 여러 컨테이너 구성은 InfraFit deploy_units 로만 읽는다. 인벤토리가 실패했는데 compose·k8s 에 서비스가 여럿이면
    # 어떤 컨테이너를 같이 띄워야 하는지 몰라 배포할 수 없다
    unknown_multi = units.from_scan(scan) is None and bool(scan["compose_services"][1:] or scan["k8s_dirs"])
    if unknown_multi:
        warnings.append("InfraFit 인벤토리가 없어 여러 컨테이너 구성(compose·k8s)을 읽지 못했습니다. 다시 분석해 주세요.")
    supported = rec.supported and not unknown_multi
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


def _health(rec: LLMRecommendation, notes: list[str]) -> str:
    health = rec.health_path if rec.health_path.startswith("/") else "/" + rec.health_path
    if len(health) > 200 or any(ch in health for ch in " \n\t"):
        notes.append("health_path 형식 오류 → '/'")
        health = "/"
    return health


def _env(rec: LLMRecommendation, notes: list[str]) -> tuple[dict, list[str]]:
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
    return env, secrets_needed


def _clues(rec: LLMRecommendation, src: source.SourceTree, notes: list[str]) -> list[dict]:
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
    return clues


# ---------------- 컨테이너 여러 개 (ec2_compose) ----------------

def validate_multi(rec: LLMRecommendation, src: source.SourceTree, scan: dict, du: dict) -> tuple[dict, list[str]]:
    """deploy_units 를 코드로 검사하고, LLM 보완(unit_fixes)을 다시 검사해 반영한다.
    supported=false 는 실행할 수 없는 컨테이너가 있거나 InfraFit 후보가 없을 때만."""
    notes: list[str] = []
    warnings = list(dict.fromkeys([*scan["warnings"], *rec.warnings]))
    u, check_notes, problems = units.check(du, src)
    notes += check_notes
    applied, rejected = units.apply_fixes(u, rec.unit_fixes, src)
    notes += rejected
    problems += units.final_problems(u)
    run_notes, run_secrets = units.prepare_run(u, src)
    if not problems:
        try:
            compose.render(u)
        except AgentError as e:
            problems.append(f"compose 를 만들 수 없습니다: {e.message}")

    target = MULTI_TARGET
    if not catalog.is_deployable(target):
        problems.append(f"컨테이너 여러 개를 실행하는 {target} 가 지금 배포 가능 목록(PAWPLOY_DEPLOYABLE)에 없습니다")
    if rec.target != target:
        notes.append(f"모델 추천 {rec.target} → 컨테이너가 여러 개라 {target} 로 정함")
    if not rec.supported:
        notes.append("모델은 supported=false 로 봤지만, 여러 컨테이너는 코드 검사 결과로만 판단함")
    inv_summary = (scan.get("inventory") or {}).get("summary") or {}
    reco = inv_summary.get("recommendation") or {}
    if reco.get("no_feasible"):
        problems.append("InfraFit: 조건을 모두 만족하는 컴퓨트 후보가 없습니다")
    services = [*u["containers"], *u["datastores"]]
    warnings += _infrafit_compute_warnings(reco, u, inv_summary)
    if rec.size == "micro" and len(services) > 2:
        warnings.append(f"컨테이너 {len(services)}개를 t3.micro(메모리 1GB)에 띄웁니다. 메모리가 모자라면 medium 을 고르세요.")

    health = _health(rec, notes)
    env, secrets_needed = _env(rec, notes)
    clues = _clues(rec, src, notes)
    names = ", ".join(s["id"] for s in services[:8])
    llm_fit = {c.target: c for c in rec.candidates}
    first = llm_fit.get(target) or llm_fit.get("aws_ec2")
    candidates = [_candidate(target, first.fit if first else None, "추천",
                             f"컨테이너 {len(services)}개({names})를 서버 1대에서 Docker Compose 로 함께 실행", rec.size)]
    seen = {target}
    for c in sorted(rec.candidates, key=lambda c: -c.fit):
        if c.target in seen:
            continue
        seen.add(c.target)
        candidates.append(_candidate(c.target, c.fit, c.verdict, c.why, rec.size))
    for tid in catalog.TARGETS:
        if tid not in seen:
            candidates.append(_candidate(tid, None, "부적합", "모델이 평가하지 않음", rec.size))
    candidates = candidates[:MAX_CANDIDATES]
    for i, c in enumerate(candidates, 1):
        c["rank"] = i
        if i > 1:                                  # 컨테이너 하나만 받는 대상: 이 앱은 그대로 못 띄움
            c["deployable"] = False
            c["why"] = f"컨테이너가 {len(services)}개라 이 대상 하나로는 그대로 띄울 수 없음. {c['why']}"

    t = catalog.TARGETS[target]
    warnings += problems + applied + units.review_warnings(u) + run_notes
    return {
        "cloud": t["cloud"], "architecture": t["architecture"], "container_port": (u.get("entry") or {}).get("port"),
        "size": rec.size, "health_path": health, "env": env, "reason": rec.reason,
        "target": target, "label": t["label"], "summary": rec.summary,
        "required_secrets": sorted((set(secrets_needed) - _generated_env(u) - _filled_env(u)) | set(run_secrets)),
        "permissions": t["permissions"],
        "cost": cost.estimate(target, rec.size), "clues": clues, "candidates": candidates,
        "supported": not problems, "warnings": list(dict.fromkeys(warnings)),
        "deploy_units": u,
    }, notes


def _generated_env(u: dict) -> set[str]:
    """저장소 비밀번호(모듈이 만드는 무작위 값, 또는 코드에 적혀 있어 그대로 쓰는 프로젝트 값)로 채우는 환경변수 이름.
    사용자가 넣을 비밀값이 아니다."""
    return {name for svc in [*u["containers"], *u["datastores"]] for name in (svc.get("run") or {}).get("credential_env") or []}


def _filled_env(u: dict) -> set[str]:
    """compose 에 값이 이미 들어가는 환경변수 이름 (예: REDIS_URL=redis://redis:6379/0). 사용자에게 다시 묻지 않는다.
    다른 서비스에서 같은 이름을 비밀값으로 받는 곳이 있으면 빼지 않는다."""
    filled, asked = set(), set()
    for svc in [*u["containers"], *u["datastores"]]:
        for name, parts in ((svc.get("run") or {}).get("env") or {}).items():
            (asked if any(isinstance(p, dict) and "secret" in p for p in parts) else filled).add(name)
    return filled - asked


def _engine(component) -> str | None:
    """'ca:unspecified/redis/default' → 'redis', 'qu:unspecified/redis-streams/default' → 'redis'.
    라이브러리(qu:lib/celery)·파일 DB(ds:local/sqlite)는 컨테이너가 아니라 None."""
    parts = str(component or "").split("/")
    if len(parts) < 2 or parts[0].endswith(":lib") or parts[0].endswith(":local"):
        return None
    return parts[1].split("-")[0] or None


def _infrafit_compute_warnings(reco: dict, u: dict, inv_summary: dict) -> list[str]:
    """InfraFit 추천과 이번 배포 묶음(deploy_units)이 다른 점. 코드가 쓰는 저장소가 묶음에 없으면 그것부터 알린다."""
    count = len(u["containers"]) + len(u["datastores"])
    rec = reco.get("recommended") or {}
    assignment = rec.get("assignment") or {}
    compute = next((c for c in assignment.values() if str(c).startswith("cp:")), None)
    out = []
    # 저장소 컨테이너가 묶음에 있는지는 InfraFit 인벤토리의 저장소(id·엔진)와 deploy_units.datastores 를 맞춰 본다
    found = {d["id"]: d for d in inv_summary.get("datastores") or [] if d.get("status") == "confirmed"}
    bundled = {d["datastore"] for d in u["datastores"] if d.get("datastore")}
    engines = {_engine(found[i].get("component")) for i in bundled if i in found}
    images = {str(d.get("image") or "").split(":")[0].rsplit("/", 1)[-1] for d in u["datastores"]} - {""}

    def in_bundle(sid: str, eng: str | None) -> bool:    # 이미지 이름은 postgres ↔ postgresql 처럼 앞부분이 같으면 같은 엔진
        return sid in bundled or eng in engines or any(eng and (eng.startswith(i) or i.startswith(eng)) for i in images)

    missing = {}
    for sid, d in found.items():
        eng = _engine(d.get("component"))
        if eng and not in_bundle(sid, eng):
            missing.setdefault(eng, d)
    for eng, d in missing.items():
        at = ", ".join(d.get("at") or [])[:80]
        out.append(f"코드가 {eng} 를 쓰는데({at}) 이번 배포 묶음에 {eng} 컨테이너가 없습니다 (compose 에 없는 저장소는 "
                   f"띄우지 않음). 접속 주소를 환경변수로 따로 넣지 않으면 앱이 localhost 로 접속하다 실패합니다. "
                   f"docker-compose.yml 에 {eng} 서비스를 추가하고 다시 분석하세요.")
    if compute and compute != COMPOSE_COMPUTE:
        out.append(f"InfraFit 1순위 컴퓨트는 {rec.get('target') or '?'}({compute})지만, 컨테이너 {count}개를 그대로 함께 "
                   f"띄울 수 있는 배포 대상은 {MULTI_TARGET} 뿐이라 이것으로 정했습니다.")
    managed = sorted(c for scope, c in assignment.items()
                     if not str(c).startswith("cp:") and in_bundle(scope, _engine((found.get(scope) or {}).get("component"))))
    if managed:
        out.append(f"InfraFit 은 데이터 저장소를 관리형({', '.join(managed[:4])})으로 추천했지만, 이번 배포는 같은 서버의 "
                   "컨테이너로 띄웁니다 (서버를 지우면 데이터도 사라짐).")
    return out


def _run_multi(payload: dict, brain, store: Store, src, scan: dict, rec, du: dict, analysis_id: str) -> dict:
    project_id = payload["project_id"]
    recommendation, notes = validate_multi(rec, src, scan, du)
    u = recommendation["deploy_units"]
    ports = units.image_ports(u)
    files = {"dockerignore": buildfiles.DOCKERIGNORE, "buildspec.yml": buildfiles.buildspec_images()}
    images, df_notes = {}, []
    for img in u["images"]:
        iid = img["id"]
        if img.get("dockerfile"):              # 프로젝트 Dockerfile 은 그대로 쓴다 (고치지 않음)
            images[iid] = {"dockerfile": img["dockerfile"], "generated": False, "context": img["context"],
                           "target": img.get("target"), "port": ports.get(iid)}
            df_notes.append(f"{iid}: 프로젝트 Dockerfile 그대로 사용 ({img['dockerfile']})")
            continue
        if img.get("base"):                    # 레지스트리 이미지 + 저장소 설정 파일 (units._bake, LLM 없음)
            name = buildfiles.generated_name(iid)
            files[name] = buildfiles.baked_dockerfile(img["base"], img["copies"])
            images[iid] = {"dockerfile": name, "generated": True, "context": img["context"], "target": None,
                           "port": ports.get(iid)}
            df_notes.append(f"{iid}: {img['base']} + 저장소 파일 {len(img['copies'])}개 COPY")
            continue
        out = brain.image_dockerfile(src, scan, img, units.users_of(u, iid))
        text, fixes = buildfiles.normalize_dockerfile(out.dockerfile, ports.get(iid), lambda_adapter=False)
        name = buildfiles.generated_name(iid)
        files[name] = text
        images[iid] = {"dockerfile": name, "generated": True, "context": img["context"], "target": None,
                       "port": ports.get(iid)}
        df_notes += [f"{iid}: {n}" for n in [*out.notes, *fixes]]
    files[buildfiles.IMAGES_JSON], files[buildfiles.IMAGES_TSV] = buildfiles.images_manifest(images)

    build_prefix = f"projects/{project_id}/build/{analysis_id}/attempt-1/"
    for name, text in files.items():
        store.put_text(build_prefix + name, text)
    recommendation["dockerfile_notes"] = df_notes
    recommendation.update({"project_id": project_id, "analysis_id": analysis_id,
                           "commit_sha": payload.get("commit_sha"), "model_id": config.MODEL_ID,
                           "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    rec_uri = store.put_json(f"projects/{project_id}/analysis/{analysis_id}/recommendation.json", recommendation)
    return {
        "status": "ok", "mode": "analyze", "project_id": project_id, "analysis_id": analysis_id,
        "recommendation": recommendation, "recommendation_uri": rec_uri,
        "build_files": {"attempt": 1, "uri_prefix": store.prefix_uri(build_prefix), "images": images,
                        "buildspec": files["buildspec.yml"]},
        "validation_notes": notes,
    }


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
