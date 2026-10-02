"""fix_build 모드 (그림 11~13): 빌드 실패 로그를 보고 Dockerfile 수정.

buildspec 은 고정 템플릿이라 고치지 않는다. Dockerfile로 못 고치는 실패(권한, 로그인 등)는
fixable=false 로 돌려줘서 Main Server가 사람에게 넘기게 한다.
"""
from . import buildfiles, config, source
from .analyze import _need
from .errors import AgentError
from .scan import scan as run_scan
from .storage import Store


def run(payload: dict, brain, store: Store) -> dict:
    project_id = _need(payload, "project_id")
    analysis_id = _need(payload, "analysis_id")
    source_uri = _need(payload, "source_uri")
    dockerfile = _need(payload, "dockerfile")
    build_log = str(payload.get("build_log") or "")
    failed_phase = str(payload.get("failed_phase") or "BUILD")
    attempt = int(payload.get("attempt") or 1)          # 이번이 몇 번째 수정 시도인지 (1부터)

    base = {"mode": "fix_build", "project_id": project_id, "analysis_id": analysis_id, "attempt": attempt}
    if attempt > config.MAX_ATTEMPTS:
        return {**base, "status": "give_up",
                "reason": f"자동 수정 {config.MAX_ATTEMPTS}회를 넘었습니다. 사람이 확인해야 합니다."}
    if not build_log.strip():
        raise AgentError("bad_request", "build_log 가 비어 있습니다")

    src = source.load(source_uri)
    scan = run_scan(src)
    fix = brain.fix_dockerfile(src, scan, dockerfile, build_log, failed_phase)
    if not fix.fixable:
        return {**base, "status": "give_up", "reason": fix.cause, "fixable": False}

    new_df, fixes = buildfiles.normalize_dockerfile(fix.dockerfile, fix.container_port)
    if new_df.strip() == buildfiles.normalize_dockerfile(dockerfile, fix.container_port)[0].strip():
        return {**base, "status": "give_up", "reason": "모델이 Dockerfile을 바꾸지 못했습니다: " + fix.cause}

    prefix = f"projects/{project_id}/build/{analysis_id}/attempt-{attempt + 1}/"
    store.put_text(prefix + buildfiles.DOCKERFILE_NAME, new_df)
    store.put_text(prefix + "dockerignore", buildfiles.DOCKERIGNORE)
    store.put_text(prefix + "buildspec.yml", buildfiles.buildspec())
    return {**base, "status": "ok", "cause": fix.cause, "changes": list(fix.changes) + fixes,
            "container_port": fix.container_port,
            "build_files": {"attempt": attempt + 1, "uri_prefix": store.prefix_uri(prefix),
                            "dockerfile": new_df}}
