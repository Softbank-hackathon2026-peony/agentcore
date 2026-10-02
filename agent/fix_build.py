"""fix_build 모드 (그림 11~13): 빌드 실패 로그를 보고 Dockerfile 수정.

buildspec 은 고정 템플릿이라 고치지 않는다. Dockerfile로 못 고치는 실패(권한, 로그인 등)는
fixable=false 로 돌려줘서 Main Server가 사람에게 넘기게 한다.

컨테이너가 여러 개면 `image_id` 로 실패한 이미지 하나만 고친다. 그 이미지가 프로젝트 Dockerfile 을 쓰고 있었으면
소스는 건드리지 않고 고친 사본(Dockerfile.pawploy.<이미지 id>)을 새 attempt 에 두고 그것으로 빌드하게 바꾼다.
다른 이미지는 이전 attempt 그대로 새 attempt 로 옮긴다 (buildspec 은 한 폴더만 본다).
"""
import json

from . import buildfiles, config, source
from .analyze import _need
from .errors import AgentError
from .scan import scan as run_scan
from .storage import Store


def run(payload: dict, brain, store: Store) -> dict:
    project_id = _need(payload, "project_id")
    analysis_id = _need(payload, "analysis_id")
    source_uri = _need(payload, "source_uri")
    image_id = payload.get("image_id")
    dockerfile = payload.get("dockerfile") if image_id else _need(payload, "dockerfile")
    build_log = str(payload.get("build_log") or "")
    failed_phase = str(payload.get("failed_phase") or "BUILD")
    attempt = int(payload.get("attempt") or 1)          # 이번이 몇 번째 수정 시도인지 (1부터)

    base = {"mode": "fix_build", "project_id": project_id, "analysis_id": analysis_id, "attempt": attempt}
    if image_id:
        base["image_id"] = image_id
    if attempt > config.MAX_ATTEMPTS:
        return {**base, "status": "give_up",
                "reason": f"자동 수정 {config.MAX_ATTEMPTS}회를 넘었습니다. 사람이 확인해야 합니다."}
    if not build_log.strip():
        raise AgentError("bad_request", "build_log 가 비어 있습니다")

    src = source.load(source_uri)
    scan = run_scan(src, inventory=False)   # 빌드 수정에는 인벤토리가 필요 없다
    if image_id:
        return _fix_image(payload, brain, store, src, scan, base, str(image_id), dockerfile, build_log, failed_phase)

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


def _fix_image(payload, brain, store: Store, src, scan, base: dict, image_id: str, dockerfile, build_log: str,
               failed_phase: str) -> dict:
    project_id, analysis_id, attempt = base["project_id"], base["analysis_id"], base["attempt"]
    cur = f"projects/{project_id}/build/{analysis_id}/attempt-{attempt}/"
    images = payload.get("images")          # analyze 응답의 build_files.images (없으면 저장된 images.json)
    if not isinstance(images, dict):
        try:
            images = json.loads(store.get_text(cur + buildfiles.IMAGES_JSON))
        except Exception:  # noqa: BLE001 — 저장소 종류마다 예외가 달라서
            raise AgentError("bad_request", f"이미지 목록이 없습니다 (images 를 넘기거나 {cur}{buildfiles.IMAGES_JSON} 필요)") from None
    img = images.get(image_id)
    if not isinstance(img, dict) or not img.get("dockerfile"):
        raise AgentError("bad_request", f"이미지 목록에 없는 image_id: {image_id!r} (있는 것: {sorted(images)})")
    if not dockerfile:                      # 실패한 Dockerfile: 만든 것이면 빌드 파일 폴더, 프로젝트 것이면 소스에서
        dockerfile = _read(store, cur + img["dockerfile"]) if img.get("generated") else src.read_text(img["dockerfile"])
    port = img.get("port") if isinstance(img.get("port"), int) else None

    fix = brain.fix_dockerfile(src, scan, dockerfile, build_log, failed_phase, image={"id": image_id, **img})
    if not fix.fixable:
        return {**base, "status": "give_up", "reason": fix.cause, "fixable": False}
    new_df, fixes = buildfiles.normalize_dockerfile(fix.dockerfile, port, lambda_adapter=False)
    if new_df.strip() == buildfiles.normalize_dockerfile(dockerfile, port, lambda_adapter=False)[0].strip():
        return {**base, "status": "give_up", "reason": "모델이 Dockerfile을 바꾸지 못했습니다: " + fix.cause}

    name = buildfiles.generated_name(image_id)
    new_images = {k: dict(v) for k, v in images.items()}
    new_images[image_id] = {**img, "dockerfile": name, "generated": True}
    if not img.get("generated"):            # 프로젝트 Dockerfile 은 고치지 않고 사본으로 바꿔 빌드
        new_images[image_id]["override_of"] = img["dockerfile"]
        fixes.append(f"프로젝트 Dockerfile({img['dockerfile']})은 그대로 두고 고친 사본 {name} 으로 빌드")
    prefix = f"projects/{project_id}/build/{analysis_id}/attempt-{attempt + 1}/"
    carried = []
    for other, v in images.items():         # 다른 이미지의 생성 Dockerfile 은 이전 attempt 에서 그대로 옮긴다
        if other != image_id and v.get("generated"):
            store.put_text(prefix + v["dockerfile"], _read(store, cur + v["dockerfile"]))
            carried.append(other)
    store.put_text(prefix + name, new_df)
    images_json, images_tsv = buildfiles.images_manifest(new_images)
    store.put_text(prefix + buildfiles.IMAGES_JSON, images_json)
    store.put_text(prefix + buildfiles.IMAGES_TSV, images_tsv)
    store.put_text(prefix + "dockerignore", buildfiles.DOCKERIGNORE)
    store.put_text(prefix + "buildspec.yml", buildfiles.buildspec_images())
    return {**base, "status": "ok", "cause": fix.cause, "changes": list(fix.changes) + fixes,
            "build_files": {"attempt": attempt + 1, "uri_prefix": store.prefix_uri(prefix), "images": new_images,
                            "dockerfile": new_df, "carried_over": carried}}


def _read(store: Store, key: str) -> str:
    try:
        return store.get_text(key)
    except Exception:  # noqa: BLE001
        raise AgentError("bad_request", f"이전 빌드 파일을 읽을 수 없습니다: {key}") from None
