"""Dockerfile 검사·보정 + buildspec/.dockerignore 생성.

- Dockerfile 내용은 LLM이 만들지만, 아래 규칙은 코드가 강제한다.
  * 마지막 스테이지에 Lambda Web Adapter 포함 (빌드가 사용자 선택 전에 돌아서 EC2·Lambda·Cloud Run 공용 이미지여야 함)
  * ENV PORT / EXPOSE 를 container_port 로 맞춤
  * 비밀 파일(.env, 키 파일)을 COPY/ADD 하지 않음
- buildspec 은 LLM이 만들지 않는다. CodeBuild 안에서 임의 셸 명령이 실행되는 파일이라
  정해진 템플릿을 코드로 생성한다 (프롬프트 인젝션 방지).
- 컨테이너가 여러 개면(ec2_compose) 이미지마다 빌드한다. 이미지 목록은 코드가 검사한 images.tsv 로 넘기고
  buildspec 은 그 목록을 도는 고정 템플릿(buildspec_images)이다. 프로젝트 Dockerfile 은 그대로 쓰고,
  없을 때만 LLM 이 만든 Dockerfile.pawploy.<이미지 id> 를 쓴다.
"""
import re

from .errors import AgentError
from .source import SECRET_PATTERNS

LWA_IMAGE = "public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0"   # Terraform-worker sample-app 과 같은 버전
LWA_LINE = f"COPY --from={LWA_IMAGE} /lambda-adapter /opt/extensions/lambda-adapter"
DOCKERFILE_NAME = "Dockerfile.pawploy"
MAX_DOCKERFILE_CHARS = 8000
IMAGES_JSON = "images.json"      # 이미지 목록 (사람·fix_build 용)
IMAGES_TSV = "images.tsv"        # 같은 목록을 buildspec 이 읽는 모양으로 (id, 컨텍스트, Dockerfile, 단계, 생성 여부)
_TSV_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")       # Worker job.IMAGE_ID_RE 와 같음
_TSV_PATH = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./@+-]{0,250}$")  # 탭·줄바꿈·공백 없음, - 로 시작하지 않음

# 생성한 Dockerfile 은 <Dockerfile>.dockerignore 로 이 규칙만 쓴다 (BuildKit 이 프로젝트 .dockerignore 대신 읽음).
# 프로젝트 .dockerignore 가 생성 Dockerfile 이 COPY 하는 경로(examples/, *.json 등)를 빼서 빌드가 깨지는 일을 막기 위해서다.
# 프로젝트 Dockerfile 을 그대로 쓸 때만 프로젝트 .dockerignore 뒤에 덧붙인다 (buildspec_images).
DOCKERIGNORE = "\n".join([
    "# Pawploy 규칙: 비밀·불필요 파일은 이미지에 넣지 않는다",
    "**/.git", "**/node_modules", "**/__pycache__", "**/.venv", "**/venv",
    "**/.env", "**/.env.*", "!**/.env.example", "**/*.pem", "**/*.key", "**/id_rsa*", "**/*.tfstate*",
    "**/credentials",
]) + "\n"

_SECRET_COPY = re.compile(r"^\s*(COPY|ADD)\b(.*)$", re.I)
_FROM = re.compile(r"^\s*FROM\s+", re.I)


def generated_name(image_id: str) -> str:
    """여러 이미지 중 하나를 위해 만든 Dockerfile 이름 (빌드 파일 폴더 안, CodeBuild 에서는 소스 루트에 둔다)."""
    return f"{DOCKERFILE_NAME}.{image_id}"


def normalize_dockerfile(text: str, port: int | None, lambda_adapter: bool = True) -> tuple[str, list[str]]:
    """규칙을 강제한 Dockerfile과, 코드가 고친 내용 목록을 돌려준다.
    여러 컨테이너(ec2_compose) 이미지는 Lambda 에서 돌지 않으므로 lambda_adapter=False, 포트를 모르면 port=None."""
    if not text or not text.strip():
        raise AgentError("dockerfile_invalid", "Dockerfile이 비어 있습니다")
    if len(text) > MAX_DOCKERFILE_CHARS:
        raise AgentError("dockerfile_invalid", "Dockerfile이 너무 깁니다")
    text = text.replace("\r\n", "\n").strip("\n") + "\n"
    lines = text.split("\n")
    from_idx = [i for i, l in enumerate(lines) if _FROM.match(l)]
    if not from_idx:
        raise AgentError("dockerfile_invalid", "FROM 이 없습니다")
    fixes: list[str] = []

    for l in lines:
        m = _SECRET_COPY.match(l)
        if m and _mentions_secret(m.group(2)):
            raise AgentError("dockerfile_unsafe", f"비밀 파일을 이미지에 복사하는 줄이 있습니다: {l.strip()}")

    # Lambda Web Adapter: 마지막 스테이지에 없으면 FROM 바로 아래 추가
    last = from_idx[-1]
    if lambda_adapter and not any("aws-lambda-adapter" in l for l in lines[last:]):
        lines.insert(last + 1, LWA_LINE)
        fixes.append("Lambda Web Adapter 줄을 추가함 (EC2·Cloud Run에서는 무시됨)")

    if port is None:
        return "\n".join(lines).strip("\n") + "\n", fixes

    # PORT / EXPOSE
    body = "\n".join(lines)
    if not re.search(rf"^\s*ENV\s+PORT[= ]{port}\b", body, re.M | re.I):
        lines = [l for l in lines if not re.match(r"^\s*ENV\s+PORT[= ]", l, re.I)]
        lines.insert(_insert_pos(lines), f"ENV PORT={port}")
        fixes.append(f"ENV PORT={port} 로 맞춤")
    if not re.search(rf"^\s*EXPOSE\s+{port}\b", "\n".join(lines), re.M | re.I):
        lines = [l for l in lines if not re.match(r"^\s*EXPOSE\s+", l, re.I)]
        lines.insert(_insert_pos(lines), f"EXPOSE {port}")
        fixes.append(f"EXPOSE {port} 로 맞춤")

    return "\n".join(lines).strip("\n") + "\n", fixes


def _insert_pos(lines: list[str]) -> int:
    """마지막 CMD/ENTRYPOINT 바로 앞 (없으면 맨 끝)."""
    for i in range(len(lines) - 1, -1, -1):
        if re.match(r"^\s*(CMD|ENTRYPOINT)\b", lines[i], re.I):
            return i
    return len(lines)


def _mentions_secret(args: str) -> bool:
    import fnmatch
    for tok in re.split(r"[\s,\[\]\"']+", args):
        base = tok.rstrip("/").split("/")[-1].lower()
        if base and base not in (".", "*") and any(fnmatch.fnmatch(base, p) for p in SECRET_PATTERNS):
            return True
    return False


def buildspec() -> str:
    """CodeBuild buildspec (고정 템플릿).

    Main Server가 start_build 때 넘겨야 하는 환경변수:
      SOURCE_URI        소스 스냅샷 (s3://.../ 폴더 또는 .zip/.tar.gz)
      BUILD_FILES_URI   이 에이전트가 저장한 빌드 파일 폴더 (s3://.../build/<id>/attempt-N/)
      ECR_REPO_URI      <계정>.dkr.ecr.<리전>.amazonaws.com/<저장소>
      IMAGE_TAG         예: <project_id>-<commit_sha 앞 12자리>
      GCP_AR_REPO       (선택) <리전>-docker.pkg.dev/<프로젝트>/<저장소> — 비우면 GCP push 생략
      GCP_SA_KEY_SECRET (선택) GCP 서비스계정 키가 든 Secrets Manager 이름
    결과(exported-variables): ECR_IMAGE_URI, GCP_IMAGE_URI (둘 다 @sha256 digest 고정)
    """
    return f"""version: 0.2
env:
  exported-variables:
    - ECR_IMAGE_URI
    - GCP_IMAGE_URI
phases:
  pre_build:
    commands:
      - mkdir -p /tmp/src && cd /tmp/src
      - case "$SOURCE_URI" in */) aws s3 cp --recursive "$SOURCE_URI" . ;; *.zip) aws s3 cp "$SOURCE_URI" /tmp/src.zip && unzip -q /tmp/src.zip -d . ;; *) aws s3 cp "$SOURCE_URI" /tmp/src.tgz && tar -xzf /tmp/src.tgz -C . ;; esac
      - if [ "$(ls -A | wc -l)" = "1" ] && [ -d "$(ls -A)" ]; then cd "$(ls -A)"; fi
      - aws s3 cp "${{BUILD_FILES_URI}}{DOCKERFILE_NAME}" ./{DOCKERFILE_NAME}
      - aws s3 cp "${{BUILD_FILES_URI}}dockerignore" ./{DOCKERFILE_NAME}.dockerignore
      - echo "$PWD" > /tmp/build_dir
      - aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${{ECR_REPO_URI%%/*}}"
      - if [ -n "$GCP_AR_REPO" ]; then aws secretsmanager get-secret-value --secret-id "$GCP_SA_KEY_SECRET" --query SecretString --output text | docker login -u _json_key --password-stdin "https://${{GCP_AR_REPO%%/*}}"; fi
  build:
    commands:
      - cd "$(cat /tmp/build_dir)"
      - DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -f {DOCKERFILE_NAME} -t "$ECR_REPO_URI:$IMAGE_TAG" .
  post_build:
    commands:
      - test "$CODEBUILD_BUILD_SUCCEEDING" = "1"
      - docker push "$ECR_REPO_URI:$IMAGE_TAG"
      - export ECR_IMAGE_URI="$(docker inspect --format='{{{{index .RepoDigests 0}}}}' "$ECR_REPO_URI:$IMAGE_TAG")"
      - if [ -n "$GCP_AR_REPO" ]; then docker tag "$ECR_REPO_URI:$IMAGE_TAG" "$GCP_AR_REPO:$IMAGE_TAG" && docker push "$GCP_AR_REPO:$IMAGE_TAG" && export GCP_IMAGE_URI="$(docker inspect --format='{{{{range .RepoDigests}}}}{{{{println .}}}}{{{{end}}}}' "$GCP_AR_REPO:$IMAGE_TAG" | grep "$GCP_AR_REPO" | head -1)"; fi
      - echo "ECR_IMAGE_URI=$ECR_IMAGE_URI GCP_IMAGE_URI=$GCP_IMAGE_URI"
"""


def images_manifest(images: dict[str, dict]) -> tuple[str, str]:
    """build_files.images → (images.json, images.tsv). 셸에 들어가는 값이라 형식을 코드로 다시 확인한다."""
    rows = []
    for iid, img in images.items():
        ctx = img.get("context") or "."
        values = [iid, ctx, img["dockerfile"], img.get("target") or "-"]
        paths_ok = all(_TSV_PATH.match(v) and ".." not in v.split("/") for v in values[1:3])
        if not _TSV_ID.match(iid) or not paths_ok or not (values[3] == "-" or _TSV_ID.match(values[3])):
            raise AgentError("build_files_invalid", f"이미지 {iid!r} 의 빌드 정보 형식이 맞지 않습니다: {values}")
        rows.append("\t".join(values + ["true" if img.get("generated") else "false"]))
    import json
    return json.dumps(images, ensure_ascii=False, indent=2) + "\n", "\n".join(rows) + "\n"


def buildspec_images() -> str:
    """여러 이미지 CodeBuild buildspec (고정 템플릿). 이미지 목록은 BUILD_FILES_URI 의 images.tsv.

    Main Server가 start_build 때 넘겨야 하는 환경변수: SOURCE_URI, BUILD_FILES_URI, ECR_REPO_URI, IMAGE_TAG (buildspec() 과 같음)
    이미지마다 태그 <IMAGE_TAG>-<이미지 id> 로 빌드·푸시한다 (ec2_compose 는 AWS 만이라 GCP push 는 없음).
    결과(exported-variables): IMAGE_DIGESTS = {"<이미지 id>": "<ECR_REPO_URI>@sha256:...", ...} (JSON 한 줄)
    """
    return """version: 0.2
env:
  shell: bash
  exported-variables:
    - IMAGE_DIGESTS
phases:
  pre_build:
    commands:
      - mkdir -p /tmp/src && cd /tmp/src
      - case "$SOURCE_URI" in */) aws s3 cp --recursive "$SOURCE_URI" . ;; *.zip) aws s3 cp "$SOURCE_URI" /tmp/src.zip && unzip -q /tmp/src.zip -d . ;; *) aws s3 cp "$SOURCE_URI" /tmp/src.tgz && tar -xzf /tmp/src.tgz -C . ;; esac
      - if [ "$(ls -A | wc -l)" = "1" ] && [ -d "$(ls -A)" ]; then cd "$(ls -A)"; fi
      - aws s3 cp --recursive "${BUILD_FILES_URI}" /tmp/pawploy/
      - echo "$PWD" > /tmp/build_dir
      - aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${ECR_REPO_URI%%/*}"
  build:
    commands:
      - cd "$(cat /tmp/build_dir)"
      - fail=0; while IFS=$'\\t' read -r id ctx df target gen; do if [ "$gen" = "true" ]; then cp "/tmp/pawploy/$df" "./$df"; cp /tmp/pawploy/dockerignore "./$df.dockerignore"; else cat /tmp/pawploy/dockerignore >> "$ctx/.dockerignore"; if [ -f "$df.dockerignore" ]; then cat /tmp/pawploy/dockerignore >> "$df.dockerignore"; fi; fi; tgt=(); if [ "$target" != "-" ]; then tgt=(--target "$target"); fi; echo "[pawploy] build $id ($df @ $ctx)"; DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -f "$df" "${tgt[@]}" -t "$ECR_REPO_URI:$IMAGE_TAG-$id" "$ctx" || { fail=1; break; }; done < /tmp/pawploy/IMAGES_TSV; test "$fail" = 0
  post_build:
    commands:
      - test "$CODEBUILD_BUILD_SUCCEEDING" = "1"
      - cd "$(cat /tmp/build_dir)"
      - fail=0; sep=""; out="{"; while IFS=$'\\t' read -r id ctx df target gen; do docker push "$ECR_REPO_URI:$IMAGE_TAG-$id" || { fail=1; break; }; d="$(docker inspect --format='{{index .RepoDigests 0}}' "$ECR_REPO_URI:$IMAGE_TAG-$id")"; out="$out$sep\\"$id\\":\\"$d\\""; sep=","; done < /tmp/pawploy/IMAGES_TSV; test "$fail" = 0 && export IMAGE_DIGESTS="$out}"
      - echo "IMAGE_DIGESTS=$IMAGE_DIGESTS"
""".replace("IMAGES_TSV", IMAGES_TSV)
