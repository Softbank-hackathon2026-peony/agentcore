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
import json
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


def baked_dockerfile(base: str, copies: list[dict]) -> str:
    """레지스트리 이미지에 저장소 파일(compose 의 bind mount 자리)을 넣은 Dockerfile (코드가 만듦, 빌드 컨텍스트는 저장소 루트)."""
    lines = ["# Pawploy: compose 의 저장소 파일 마운트를 이미지 안으로 옮김", f"FROM {base}"]
    lines += [f"COPY {json.dumps([c['from'], c['to']])}" for c in copies]
    return "\n".join(lines) + "\n"


def existing_dockerfile(src, preferred_port: int):
    """Reuse an unambiguous root production Dockerfile with a literal exposed TCP port.

    EXPOSE is evidence, not a guarantee that the server listens. Ambiguous ports,
    build arguments and development commands still need model inspection.
    Normalization downstream retains secret checks and the pinned Lambda adapter.
    """
    from .schemas import DockerfileOut
    if not src.exists("Dockerfile"):
        return None
    text = src.read_text("Dockerfile", limit=MAX_DOCKERFILE_CHARS + 1)
    if len(text) > MAX_DOCKERFILE_CHARS:
        return None
    lines = text.splitlines()
    stages = _instr_idx(lines, _FROM)
    if not stages or _instr_idx(lines, re.compile(r"^\s*ARG\b", re.I)):
        return None
    if re.search(r"--reload\b|--debug\b|\bnodemon\b", text):
        return None
    ports = []
    for s, e in _instructions(lines):
        if s <= stages[-1]:
            continue
        instr = _joined(lines, s, e)
        m = re.match(r"^EXPOSE\s+(.+?)(?:\s+#.*)?$", instr, re.I)
        if m:
            for token in m.group(1).split():
                if not re.fullmatch(r"[0-9]+(?:/tcp)?", token):
                    return None
                port = int(token.split('/')[0])
                if not 1 <= port <= 65535:
                    return None
                ports.append(port)
    ports = list(dict.fromkeys(ports))
    # The conventional HTTP+HTTPS pair has one clear HTTP entry point.
    port = 80 if set(ports) == {80, 443} else (preferred_port if preferred_port in ports else None)
    if port is None and len(ports) == 1:
        port = ports[0]
    if port is None:
        return None
    return DockerfileOut(dockerfile=text, container_port=port,
                         notes=[f"기존 Dockerfile 유지 (고정 포트 {port}); PORT용 서버 설정 재작성 생략"])


def normalize_dockerfile(text: str, port: int | None, lambda_adapter: bool = True) -> tuple[str, list[str]]:
    """규칙을 강제한 Dockerfile과, 코드가 고친 내용 목록을 돌려준다.
    여러 컨테이너(ec2_compose) 이미지는 Lambda 에서 돌지 않으므로 lambda_adapter=False, 포트를 모르면 port=None.
    명령 줄만 본다: \\ 로 이어진 줄과 heredoc 본문(RUN cat <<EOF ... EOF) 안의 `from x import y` 같은 줄은 명령이 아니다."""
    if not text or not text.strip():
        raise AgentError("dockerfile_invalid", "Dockerfile이 비어 있습니다")
    if len(text) > MAX_DOCKERFILE_CHARS:
        raise AgentError("dockerfile_invalid", "Dockerfile이 너무 깁니다")
    text = text.replace("\r\n", "\n").strip("\n") + "\n"
    lines = text.split("\n")
    if not _instr_idx(lines, _FROM):
        raise AgentError("dockerfile_invalid", "FROM 이 없습니다")
    fixes: list[str] = []

    for start, end in _instructions(lines):
        m = _SECRET_COPY.match(_joined(lines, start, end))
        if m and _mentions_secret(m.group(2)):
            raise AgentError("dockerfile_unsafe", f"비밀 파일을 이미지에 복사하는 줄이 있습니다: {lines[start].strip()}")

    # Lambda Web Adapter: 마지막 스테이지에 LWA_LINE 딱 하나. LLM 이 쓴 어댑터 COPY 는 이름·버전이 틀릴 수 있어서
    # (예: 없는 이미지 aws-lambda-web-adapter:0.8.4 가 정상 줄과 같이 들어가 검사를 통과함) 정확히 그 한 줄이 아니면 전부 지우고 다시 넣는다
    lines, found = _strip_lambda_adapter(lines)
    last = _instr_idx(lines, _FROM)[-1]
    if lambda_adapter:
        lines.insert(last + 1, LWA_LINE)
        if found != [(LWA_LINE, True)]:
            wrong = [s for s, _ in found if s != LWA_LINE]
            fixes.append("Lambda Web Adapter 줄을 1.1.0 하나로 맞춤"
                         + (f" (잘못된 줄 제거: {'; '.join(wrong)[:200]})" if wrong else " (EC2·Cloud Run에서는 무시됨)"))
    elif found:
        fixes.append("Lambda 에서 돌지 않는 이미지라 Lambda Web Adapter 줄을 뺌")

    if port is None:
        return "\n".join(lines).strip("\n") + "\n", fixes

    # PORT / EXPOSE (명령 줄만 보고, 명령 줄만 지운다)
    if not _instr_idx(lines, re.compile(rf"^\s*ENV\s+PORT[= ]{port}\b", re.I)):
        lines = _drop_instr(lines, re.compile(r"^\s*ENV\s+PORT[= ]", re.I))
        lines.insert(_insert_pos(lines), f"ENV PORT={port}")
        fixes.append(f"ENV PORT={port} 로 맞춤")
    if not _instr_idx(lines, re.compile(rf"^\s*EXPOSE\s+{port}\b", re.I)):
        lines = _drop_instr(lines, re.compile(r"^\s*EXPOSE\s+", re.I))
        lines.insert(_insert_pos(lines), f"EXPOSE {port}")
        fixes.append(f"EXPOSE {port} 로 맞춤")

    return "\n".join(lines).strip("\n") + "\n", fixes


_LWA_REF = re.compile(r"lambda-(web-)?adapter", re.I)
_HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _instructions(lines: list[str]) -> list[tuple[int, int]]:
    """명령마다 (첫 줄, 마지막 줄). \\ 로 이어진 줄과 heredoc 본문·끝 표시는 그 명령에 포함된다.
    빈 줄과 주석은 명령이 아니다."""
    out, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        start, pending = i, []
        while True:
            pending += [m.group(3) for m in _HEREDOC.finditer(lines[i])]
            if lines[i].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                continue
            break
        for delim in pending:                       # heredoc 본문은 끝 표시 줄까지 통째로 이 명령
            i += 1
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
        out.append((start, min(i, len(lines) - 1)))
        i += 1
    return out


def _joined(lines: list[str], start: int, end: int) -> str:
    """\\ 로 이어진 명령을 한 줄로 (heredoc 본문은 빼고 명령 줄들만)."""
    parts = [lines[start]]
    k = start
    while lines[k].rstrip().endswith("\\") and k < end:
        k += 1
        parts.append(lines[k])
    return " ".join(p.strip().rstrip("\\").strip() for p in parts)


def _instr_idx(lines: list[str], pattern: re.Pattern) -> list[int]:
    """pattern 에 맞는 명령의 첫 줄 번호들."""
    return [s for s, e in _instructions(lines) if pattern.match(_joined(lines, s, e))]


# Lambda 는 파일시스템이 읽기 전용(/tmp 만 쓰기)이고 루트가 아니라서, 시작할 때 캐시·PID 폴더를 만드는 웹 서버 이미지는 바로 죽는다
# (운영 E2E 2026-10-03: nginx:stable-alpine → mkdir /var/cache/nginx/client_temp: Read-only file system)
_LAMBDA_UNFIT_BASE = re.compile(r"(?:^|/)(nginx|nginx-unprivileged|openresty|httpd|php)(?::|@|$)", re.I)


def lambda_unfit_base(text: str | None) -> str | None:
    """마지막 스테이지 베이스 이미지가 Lambda 에서 못 뜨는 웹 서버 이미지면 그 이미지 이름, 아니면 None.
    php 는 -apache 변형만 (php-fpm·cli 는 웹 서버를 따로 띄우지 않으므로 여기서 판단하지 않는다)."""
    if not text:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    froms = [(s, e) for s, e in _instructions(lines) if _FROM.match(_joined(lines, s, e))]
    if not froms:
        return None
    parts = [p for p in _joined(lines, *froms[-1]).split()[1:] if not p.startswith("--")]
    image = parts[0] if parts else ""
    m = _LAMBDA_UNFIT_BASE.search(image)
    if not m or (m.group(1).lower() == "php" and "apache" not in image.lower()):
        return None
    return image


def _drop_instr(lines: list[str], pattern: re.Pattern) -> list[str]:
    drop = {k for s, e in _instructions(lines) if pattern.match(_joined(lines, s, e)) for k in range(s, e + 1)}
    return [l for k, l in enumerate(lines) if k not in drop]


def _strip_lambda_adapter(lines: list[str]) -> tuple[list[str], list[tuple[str, bool]]]:
    """Lambda Web Adapter 를 가져오는 COPY 명령(\\ 로 이어진 줄 포함)을 모든 스테이지에서 지운다.
    돌려주는 found = [(한 줄로 합친 명령, 마지막 스테이지였는지)]."""
    last = _instr_idx(lines, _FROM)[-1]
    found, drop = [], set()
    for s, e in _instructions(lines):
        instr = _joined(lines, s, e)
        if re.match(r"^COPY\b", instr, re.I) and _LWA_REF.search(instr):
            found.append((" ".join(instr.split()), s > last))
            drop.update(range(s, e + 1))
    return [l for k, l in enumerate(lines) if k not in drop], found


def _insert_pos(lines: list[str]) -> int:
    """마지막 CMD/ENTRYPOINT 명령 바로 앞 (없으면 맨 끝)."""
    idx = _instr_idx(lines, re.compile(r"^\s*(CMD|ENTRYPOINT)\b", re.I))
    return idx[-1] if idx else len(lines)


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
  shell: bash
  exported-variables:
    - ECR_IMAGE_URI
    - GCP_IMAGE_URI
phases:
  pre_build:
    commands:
      - |
        set -o pipefail
        login_registries() {{
        (aws ecr get-login-password --region "$AWS_REGION" | docker --config /tmp/pawploy-ecr login --username AWS --password-stdin "${{ECR_REPO_URI%%/*}}") & ecr_pid=$!
        gcp_pid=""
        if [ -n "$GCP_AR_REPO" ]; then
          (aws secretsmanager get-secret-value --secret-id "$GCP_SA_KEY_SECRET" --query SecretString --output text | docker --config /tmp/pawploy-gcp login -u _json_key --password-stdin "https://${{GCP_AR_REPO%%/*}}") & gcp_pid=$!
        fi
        failed=0
        wait "$ecr_pid" || failed=1
        if [ -n "$gcp_pid" ]; then wait "$gcp_pid" || failed=1; fi
        test "$failed" = 0
        }}
        login_registries & login_pid=$!
        mkdir -p /tmp/src && cd /tmp/src || exit 1
        case "$SOURCE_URI" in */) aws s3 cp --recursive "$SOURCE_URI" . --only-show-errors ;; *.zip) aws s3 cp "$SOURCE_URI" /tmp/src.zip --only-show-errors && unzip -q /tmp/src.zip -d . ;; *) aws s3 cp "$SOURCE_URI" /tmp/src.tgz --only-show-errors && tar -xzf /tmp/src.tgz -C . ;; esac || exit 1
        if [ "$(ls -A | wc -l)" = "1" ] && [ -d "$(ls -A)" ]; then cd "$(ls -A)" || exit 1; fi
        aws s3 cp --recursive "${{BUILD_FILES_URI}}" /tmp/pawploy/ --exclude "*" --include "Dockerfile.pawploy" --include "dockerignore" --only-show-errors || exit 1
        cp /tmp/pawploy/Dockerfile.pawploy ./Dockerfile.pawploy && cp /tmp/pawploy/dockerignore ./Dockerfile.pawploy.dockerignore || exit 1
        echo "$PWD" > /tmp/build_dir
        wait "$login_pid" || exit 1
        config_dir="${{DOCKER_CONFIG:-$HOME/.docker}}"
        mkdir -p "$config_dir" || exit 1
        configs=(/tmp/pawploy-ecr/config.json)
        if [ -f "$config_dir/config.json" ]; then configs=("$config_dir/config.json" "${{configs[@]}}"); fi
        if [ -n "$GCP_AR_REPO" ]; then configs+=(/tmp/pawploy-gcp/config.json); fi
        jq -s 'reduce .[] as $item ({{}}; . * $item)' "${{configs[@]}}" > "$config_dir/config.json.pawploy" && chmod 600 "$config_dir/config.json.pawploy" && mv "$config_dir/config.json.pawploy" "$config_dir/config.json"
  build:
    commands:
      - cd "$(cat /tmp/build_dir)"
      - DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -f Dockerfile.pawploy -t "$ECR_REPO_URI:$IMAGE_TAG" .
  post_build:
    commands:
      - test "$CODEBUILD_BUILD_SUCCEEDING" = "1"
      - |
        gcp_pid=""
        if [ -n "$GCP_AR_REPO" ]; then
          docker tag "$ECR_REPO_URI:$IMAGE_TAG" "$GCP_AR_REPO:$IMAGE_TAG" || exit 1
          docker push "$GCP_AR_REPO:$IMAGE_TAG" & gcp_pid=$!
        fi
        docker push "$ECR_REPO_URI:$IMAGE_TAG" & ecr_pid=$!
        failed=0
        wait "$ecr_pid" || failed=1
        if [ -n "$gcp_pid" ]; then wait "$gcp_pid" || failed=1; fi
        test "$failed" = 0
      - export ECR_IMAGE_URI="$(docker inspect --format='{{{{range .RepoDigests}}}}{{{{println .}}}}{{{{end}}}}' "$ECR_REPO_URI:$IMAGE_TAG" | grep -F "$ECR_REPO_URI@sha256:" | head -1)"; test -n "$ECR_IMAGE_URI"
      - if [ -n "$GCP_AR_REPO" ]; then export GCP_IMAGE_URI="$(docker inspect --format='{{{{range .RepoDigests}}}}{{{{println .}}}}{{{{end}}}}' "$GCP_AR_REPO:$IMAGE_TAG" | grep -F "$GCP_AR_REPO@sha256:" | head -1)"; test -n "$GCP_IMAGE_URI"; fi
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
