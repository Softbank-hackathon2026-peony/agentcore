"""gen_terraform (그림 18~20) / fix_terraform (그림 23~25).

AI가 만드는 것은 Terraform-worker의 `modules/<아키텍처>/` 자리에 들어갈 **모듈 하나**다.
Worker 루트 main.tf 가 provider(필수 태그·리전), backend(state), 만료 예약을 강제하고
`module "app" { source = "./modules/<아키텍처>" }` 로 이 모듈을 부른다. 그래서
- 입력 변수 6개(name, image_uri, container_port, size, env, health_path)와
- 출력 3개(endpoint, health_url, resource_id)는 반드시 지금 모듈과 같아야 하고
- provider / backend / provisioner / 외부 모듈 / 임의 파일 읽기는 금지한다.
Worker 의 IaC 정적 검사(tfworker/iac.py)와 같은 규칙을 `_check_worker_iac` 에 그대로 옮겨 두었다.
여기서 하는 검사는 1차 방어선이고, 최종 방어선은 Worker의 plan 정책 검사다.
"""
import re
from pathlib import Path

from . import config, source
from .analyze import _need
from .errors import AgentError
from .schemas import TfFile
from .storage import Store

REF_DIR = Path(__file__).with_name("tf_reference")
REFERENCES = {"ec2": ["ec2.tf", "user_data.sh.tftpl"], "lambda": ["lambda.tf"], "cloud_run": ["cloud_run.tf"]}

# ---- Worker 규격 (Terraform-worker feat/multi-cloud-a-plan 의 tfworker/policy.py · iac.py 와 같게 유지) ----
# policy.ALLOWED_TYPES
ALLOWED_RESOURCES = {
    "ec2": {"aws_security_group", "aws_iam_role", "aws_iam_role_policy_attachment",
            "aws_iam_instance_profile", "aws_instance"},
    "lambda": {"aws_iam_role", "aws_iam_role_policy_attachment", "aws_lambda_function",
               "aws_lambda_function_url", "aws_lambda_permission"},
    "cloud_run": {"google_service_account", "google_cloud_run_v2_service",
                  "google_cloud_run_v2_service_iam_member"},
}
CLOUD_OF = {"ec2": "aws", "lambda": "aws", "cloud_run": "gcp"}
# iac.ALLOWED_DATA
ALLOWED_DATA = {
    "aws": {"aws_vpc", "aws_subnets", "aws_subnet", "aws_ssm_parameter", "aws_ami", "aws_availability_zones",
            "aws_region", "aws_partition", "aws_iam_policy_document"},
    "gcp": {"google_client_config"},
}
# iac.PROVIDER_SOURCES
PROVIDER_SOURCES = {"aws": "hashicorp/aws", "gcp": "hashicorp/google"}
# policy.ALLOWED_POLICY_ARNS
ALLOWED_POLICY_ARNS = {
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
}
# iac.FORBIDDEN. Worker 는 주석까지 포함한 원문을 검사하므로 여기서도 원문에 적용한다
FORBIDDEN = [
    (r"\bprovisioner\b", "provisioner"),
    (r"\blocal-exec\b|\bremote-exec\b", "local-exec / remote-exec"),
    (r'\bprovider\s+"', "provider 블록 (Worker가 루트에서 정함)"),
    (r'\bbackend\s+"|\bcloud\s*\{', "backend / cloud 블록 (state 위치는 Worker가 정함)"),
    (r'\bmodule\s+"', "다른 module 호출 (외부 코드 다운로드 가능)"),
    (r"\bdefault_tags\b|\bdefault_labels\b", "default_tags / default_labels (필수 태그를 덮어쓸 수 있음)"),
    (r"\binline_policy\b|\bmanaged_policy_arns\b", "IAM 인라인 정책 (허용한 관리형 정책만 붙일 수 있음)"),
    (r"\baccess_token\b", "access_token (Worker의 GCP 토큰을 앱으로 흘릴 수 있음)"),
]
SSM_BLOCK_RE = re.compile(r'data\s+"aws_ssm_parameter"\s+"[\w-]+"\s*\{(.*?)\n\}', re.DOTALL)
ALLOWED_INSTANCE_TYPES = {"t3.micro", "t3.small", "t3.medium"}
MAX_LAMBDA_MEMORY_MB = 2048
MAX_LAMBDA_TIMEOUT_SEC = 60
MAX_CLOUD_RUN_MEMORY_MI = 2048    # policy.MAX_CLOUD_RUN_MEMORY_MI
MAX_CLOUD_RUN_INSTANCES = 1       # policy.MAX_CLOUD_RUN_INSTANCES
REQUIRED_VARS = ("name", "image_uri", "container_port", "size", "env", "health_path")
REQUIRED_OUTPUTS = ("endpoint", "health_url", "resource_id")
FILE_NAME_RE = re.compile(r"^(main\.tf|[a-z0-9_\-][a-z0-9_.\-]*\.tftpl)$")
MAX_FILE_CHARS = 20_000
INTERNAL_RETRIES = 2      # 우리 검사에 걸리면 같은 호출 안에서 LLM에 에러를 돌려 다시 받는 횟수


def reference(architecture: str) -> str:
    parts = []
    for name in REFERENCES[architecture]:
        parts.append(f"### 견본 파일 {name}\n```\n{(REF_DIR / name).read_text(encoding='utf-8')}\n```")
    return "\n\n".join(parts)


def check_files(files: list[TfFile], architecture: str) -> tuple[list[TfFile], list[str], list[str]]:
    """(보정된 파일, 위반 목록, 코드가 고친 내용). 위반이 있으면 쓰면 안 된다."""
    errors, fixes, out = [], [], []
    names = [f.name for f in files]
    if "main.tf" not in names:
        errors.append("main.tf 가 없습니다")
    if len(files) > 3:
        errors.append("파일은 main.tf + 템플릿 최대 2개까지입니다")
    for f in files:
        if not FILE_NAME_RE.match(f.name):
            errors.append(f"허용하지 않는 파일 이름: {f.name} (main.tf 또는 *.tftpl)")
            continue
        if len(f.content) > MAX_FILE_CHARS:
            errors.append(f"{f.name} 이 너무 깁니다")
            continue
        text = f.content.replace("\r\n", "\n")
        if f.name.endswith(".tf"):
            text, removed = _remove_blocks(text, r'provider\s+"[^"]+"')
            if removed:
                fixes.append(f"provider 블록 {removed}개 제거 (루트가 리전·태그를 강제)")
            errors += _check_tf(text, architecture, {x.name for x in files})
        out.append(TfFile(name=f.name, content=text.strip("\n") + "\n"))
    return out, errors, fixes


def _check_tf(text: str, arch: str, file_names: set[str]) -> list[str]:
    e = _check_worker_iac(text, arch)
    body = _strip_comments(text)
    # Worker 와 같이 주석까지 검사한다. Worker 보다 엄격하게 같은 모듈의 .tftpl 만 허용
    for m in re.finditer(r'\b(file\w*|templatefile)\s*\(\s*("[^"]*"|[^,)]*)', text):
        arg = m.group(2)
        ok = m.group(1) == "templatefile" and re.fullmatch(r'"\$\{path\.module\}/([a-z0-9_\-][a-z0-9_.\-]*\.tftpl)"', arg)
        if not ok or ok.group(1) not in file_names:
            e.append(f"파일 읽기는 같은 모듈의 .tftpl 템플릿만 가능합니다: {m.group(0)[:60]}")
    declared_vars = set(re.findall(r'^\s*variable\s+"([^"]+)"', body, re.M))
    extra = declared_vars - set(REQUIRED_VARS)
    if extra:
        e.append(f"Worker가 넘겨주지 않는 변수는 쓸 수 없습니다: {sorted(extra)} (locals 로 바꾸세요)")
    for it in re.findall(r'"([a-z][a-z0-9]*\.[a-z0-9]+)"', body):
        if re.fullmatch(r"(t|m|c|r|g|p|x|i|z|d|inf|trn)\d[a-z]*\.\w+", it) and it not in ALLOWED_INSTANCE_TYPES:
            e.append(f"허용하지 않는 인스턴스 타입: {it}")
    for m in re.finditer(r"\bmemory_size\s*=\s*(\d+)", body):
        if int(m.group(1)) > MAX_LAMBDA_MEMORY_MB:
            e.append(f"Lambda 메모리 {m.group(1)}MB 가 상한 {MAX_LAMBDA_MEMORY_MB}MB 초과")
    for m in re.finditer(r"\btimeout\s*=\s*(\d+)", body):
        if arch == "lambda" and int(m.group(1)) > MAX_LAMBDA_TIMEOUT_SEC:
            e.append(f"Lambda 타임아웃 {m.group(1)}초가 상한 {MAX_LAMBDA_TIMEOUT_SEC}초 초과")
    if arch == "cloud_run":
        for m in re.finditer(r"\bmax_instance_count\s*=\s*(\d+)", body):
            if not 0 < int(m.group(1)) <= MAX_CLOUD_RUN_INSTANCES:
                e.append(f"Cloud Run 최대 인스턴스 수 {m.group(1)} 는 허용하지 않음 (1~{MAX_CLOUD_RUN_INSTANCES})")
        for m in re.finditer(r'"(\d+)(Gi|Mi)"', body):
            mi = int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)
            if mi > MAX_CLOUD_RUN_MEMORY_MI:
                e.append(f"Cloud Run 메모리 {m.group(0)} 가 상한 {MAX_CLOUD_RUN_MEMORY_MI}Mi 초과")
    if "<<" not in body and _brace_balance(body) != 0:
        e.append("중괄호 { } 짝이 맞지 않습니다")
    return e


def _check_worker_iac(text: str, arch: str) -> list[str]:
    """Worker tfworker/iac.py check_code 와 같은 검사. 여기서 통과해야 Worker 21단계에서 거부되지 않는다."""
    cloud = CLOUD_OF[arch]
    e = [f"금지된 문법: {why} — 주석에도 쓰지 마세요" for pattern, why in FORBIDDEN if re.search(pattern, text)]
    for src in re.findall(r'\bsource\s*=\s*"([^"]+)"', text):
        if src != PROVIDER_SOURCES[cloud]:
            e.append(f"허용하지 않는 provider source: {src} (가능: {PROVIDER_SOURCES[cloud]})")
    allowed = ALLOWED_RESOURCES[arch]
    for rtype in sorted(set(re.findall(r'\bresource\s+"([A-Za-z0-9_]+)"', text)) - allowed):
        e.append(f"허용하지 않는 리소스 종류: {rtype} (가능: {sorted(allowed)})")
    for dtype in sorted(set(re.findall(r'\bdata\s+"([A-Za-z0-9_]+)"', text)) - ALLOWED_DATA[cloud]):
        e.append(f"허용하지 않는 data 소스: {dtype} (가능: {sorted(ALLOWED_DATA[cloud])})")
    for body in SSM_BLOCK_RE.findall(text):
        if not re.search(r'\bname\s*=\s*"/aws/service/', body):
            e.append("aws_ssm_parameter 는 AWS 공개 파라미터(/aws/service/...)만 읽을 수 있음")
    for name in REQUIRED_VARS:
        if not re.search(rf'\bvariable\s+"{name}"', text):
            e.append(f"입력 변수 {name} 가 없습니다 (Worker가 넘겨주는 값)")
    for name in REQUIRED_OUTPUTS:
        if not re.search(rf'\boutput\s+"{name}"', text):
            e.append(f"출력 {name} 가 없습니다")
    for arn in re.findall(r'\bpolicy_arn\s*=\s*"([^"]+)"', text):
        if arn not in ALLOWED_POLICY_ARNS:
            e.append(f"허용하지 않는 IAM 정책: {arn} (가능: {sorted(ALLOWED_POLICY_ARNS)})")
    if arch == "ec2" and not re.search(r'\bcpu_credits\s*=\s*"standard"', text):
        e.append('EC2 는 credit_specification { cpu_credits = "standard" } 를 유지해야 함 (추가 과금 방지)')
    if arch == "cloud_run" and not re.search(r"\bdeletion_protection\s*=\s*false\b", text):
        e.append("Cloud Run 은 deletion_protection = false 여야 함 (켜져 있으면 1시간 뒤 destroy 가 실패)")
    return e


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r'(^|\s)(#|//).*$', "", l) for l in text.split("\n"))


def _brace_balance(text: str) -> int:
    depth, in_str, esc = 0, False, False
    for ch in text:
        if in_str:
            esc = (ch == "\\" and not esc)
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return depth


def _remove_blocks(text: str, header_re: str) -> tuple[str, int]:
    """최상위 `provider "x" { ... }` 같은 블록을 중괄호 짝을 세서 지운다."""
    count = 0
    while True:
        m = re.search(rf"^\s*{header_re}\s*\{{", text, re.M)
        if not m:
            return text, count
        i, depth = m.end(), 1
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        text = text[:m.start()] + text[i:]
        count += 1


def _arch(rec: dict) -> str:
    arch = str(rec.get("architecture") or "")
    if arch not in ALLOWED_RESOURCES:
        raise AgentError("unsupported_architecture",
                         f"Terraform 생성을 지원하지 않는 아키텍처: {arch!r} (가능: {sorted(ALLOWED_RESOURCES)})")
    return arch


def _load_recommendation(payload: dict) -> dict:
    if isinstance(payload.get("recommendation"), dict):
        return payload["recommendation"]
    uri = _need(payload, "recommendation_uri")
    import json
    if uri.startswith("s3://"):
        import boto3
        b, _, k = uri[5:].partition("/")
        body = boto3.client("s3", region_name=config.REGION).get_object(Bucket=b, Key=k)["Body"].read()
        return json.loads(body)
    return json.loads(Path(uri).read_text(encoding="utf-8"))


def _save(store: Store, project_id: str, deploy_id: str, attempt: int, files: list[TfFile]) -> str | None:
    prefix = f"projects/{project_id}/deploy/{deploy_id}/attempt-{attempt}/"
    for f in files:
        store.put_text(prefix + f.name, f.content)
    return store.prefix_uri(prefix)


def gen(payload: dict, brain, store: Store) -> dict:
    project_id = _need(payload, "project_id")
    deploy_id = _need(payload, "deploy_id")
    rec = _load_recommendation(payload)
    arch = _arch(rec)
    ctx = {k: rec.get(k) for k in ("cloud", "architecture", "container_port", "size", "health_path",
                                    "summary", "reason", "required_secrets")}
    ctx["env_names"] = sorted((rec.get("env") or {}).keys())
    errors: list[str] = []
    for _ in range(INTERNAL_RETRIES + 1):
        out = brain.gen_terraform(ctx, arch, reference(arch), errors)
        files, errors, fixes = check_files(out.files, arch)
        if not errors:
            break
    else:
        return {"status": "error", "mode": "gen_terraform",
                "error": {"code": "terraform_invalid", "message": "생성된 Terraform이 검사를 통과하지 못했습니다"},
                "violations": errors}
    uri = _save(store, project_id, deploy_id, 1, files)
    return {"status": "ok", "mode": "gen_terraform", "project_id": project_id, "deploy_id": deploy_id,
            "architecture": arch, "attempt": 1, "module_uri": uri,
            "files": {f.name: f.content for f in files}, "resources": out.resources,
            "notes": list(out.notes) + fixes}


def fix(payload: dict, brain, store: Store) -> dict:
    project_id = _need(payload, "project_id")
    deploy_id = _need(payload, "deploy_id")
    arch = _arch({"architecture": _need(payload, "architecture")})
    attempt = int(payload.get("attempt") or 1)
    stage = str(payload.get("failed_stage") or "apply")
    log = str(payload.get("log") or "")
    base = {"mode": "fix_terraform", "project_id": project_id, "deploy_id": deploy_id, "attempt": attempt}
    if attempt > config.MAX_ATTEMPTS:
        return {**base, "status": "give_up",
                "reason": f"자동 수정 {config.MAX_ATTEMPTS}회를 넘었습니다. 사람이 확인해야 합니다."}
    if not log.strip():
        raise AgentError("bad_request", "log 가 비어 있습니다")

    if isinstance(payload.get("files"), dict):
        current = [TfFile(name=k, content=v) for k, v in payload["files"].items()]
    else:
        tree = source.load(_need(payload, "module_uri"))
        current = [TfFile(name=p, content=tree.read_text(p, limit=MAX_FILE_CHARS)) for p in tree.paths()]

    errors: list[str] = []
    for _ in range(INTERNAL_RETRIES + 1):
        res = brain.fix_terraform(current, arch, stage, log, reference(arch), errors)
        if not res.fixable:
            return {**base, "status": "give_up", "fixable": False, "reason": res.cause}
        files, errors, fixes = check_files(res.files, arch)
        if not errors:
            break
    else:
        return {**base, "status": "give_up", "reason": "수정본이 검사를 통과하지 못했습니다", "violations": errors}
    if {f.name: f.content.strip() for f in files} == {f.name: f.content.strip() for f in current}:
        return {**base, "status": "give_up", "reason": "모델이 코드를 바꾸지 못했습니다: " + res.cause}
    uri = _save(store, project_id, deploy_id, attempt + 1, files)
    return {**base, "status": "ok", "cause": res.cause, "changes": list(res.changes) + fixes,
            "next_attempt": attempt + 1, "module_uri": uri, "files": {f.name: f.content for f in files}}
