"""deploy_units → Terraform-worker `modules/ec2_compose` 의 compose.yaml.tftpl (코드, LLM 아님).

추천안의 `deploy_units`(units.py 가 검사·변환한 것)만 보고 만든다. 같은 입력이면 항상 같은 결과다.
- 서비스 이름 = deploy_units id 그대로 (컨테이너끼리 이 이름으로 접속)
- 빌드한 이미지는 `${images["<이미지 id>"]}`, 레지스트리 이미지(postgres:16-alpine 등)는 그대로
- 호스트 포트는 entry 컨테이너 하나만 `80:<entry 포트>`
- DB 비밀번호는 `${passwords["<저장소 id>"]}` (모듈이 random_password 로 만듦), 사용자 비밀값은 이름만 두고 값은 빈 문자열
- 사용자 값(명령·환경변수·헬스체크)은 모두 YAML 큰따옴표 문자열로 쓰고, 템플릿 문법과 겹치는 `${`·`%{` 는 `$${`·`%%{` 로 바꾼다

템플릿 값 이름은 Worker README "ec2_compose 템플릿 약속"(modules/ec2_compose/main.tf 의 templatefile 인자)과 같아야 한다.
바뀌면 TEMPLATE_VARS 만 고친다.
"""
import json
import re
import shlex

from .errors import AgentError

TEMPLATE_NAME = "compose.yaml.tftpl"
# Worker modules/ec2_compose/main.tf: templatefile("${path.module}/compose.yaml.tftpl", {images = ..., passwords = ...})
TEMPLATE_VARS = {"images": "images", "passwords": "passwords"}
PROJECT_NAME = "app"                     # Worker 약속: compose 프로젝트 이름
ENTRY_HOST_PORT = 80                     # Worker 보안 그룹·헬스체크가 보는 포트

# ---- Worker tfworker/iac.py 와 같게 유지 (tests/test_terraform_contract.py 가 비교) ----
# 템플릿(.tftpl) 안의 파일 읽기 함수 (주석 포함 원문 검사)
TEMPLATE_FILE_FUNC_RE = re.compile(
    r"\b(file|filebase64|filebase64sha\d+|fileexists|filemd5|filesha\d+|fileset|templatefile)\s*\(")
IMAGE_REF_RE = re.compile(r'\bimages\["([A-Za-z0-9_.-]+)"\]')
# compose 컨테이너가 인스턴스(호스트) 권한을 갖게 하는 설정
COMPOSE_FORBIDDEN = [
    (r"\bprivileged\s*:\s*['\"]?true", "privileged: true"),
    (r"\b(network_mode|pid|ipc|userns_mode)\s*:\s*['\"]?host\b", "network_mode/pid/ipc: host"),
    (r"docker\.sock", "Docker 소켓 마운트"),
    (r"\bcap_add\s*:", "cap_add"),
    (r"\bdevices\s*:", "devices"),
    (r"(?m)^\s*build\s*:", "build (이미지는 미리 빌드해 images 로 넘김)"),
]
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_INTERP = re.compile(r'\$\{(\w+)\["([A-Za-z0-9_.-]+)"\]\}')


def image_ref(image_id: str) -> str:
    return f'${{{TEMPLATE_VARS["images"]}["{image_id}"]}}'


def password_ref(store_id: str) -> str:
    return f'${{{TEMPLATE_VARS["passwords"]}["{store_id}"]}}'


def _lit(text: str) -> str:
    """사용자 값 → YAML 큰따옴표 안에 넣을 글자 (JSON 이스케이프는 YAML 에서도 유효) + 템플릿 문법 이스케이프."""
    return json.dumps(str(text))[1:-1].replace("${", "$${").replace("%{", "%%{")


def _scalar(parts) -> str:
    """parts: 문자열(글자 그대로) 또는 {"password": id} / {"secret": 이름}. 결과는 큰따옴표 YAML 문자열."""
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(_lit(p))
        elif isinstance(p, dict) and _ID.match(str(p.get("password") or "")):
            if out and out[-1].endswith("$"):        # "$" 바로 뒤의 ${ 는 템플릿이 $${ 로 읽는다
                raise AgentError("compose_invalid", "비밀번호 자리 바로 앞에 $ 가 있어 템플릿으로 쓸 수 없습니다")
            out.append(password_ref(p["password"]))
        elif isinstance(p, dict) and "secret" in p:
            out.append("")                             # 사용자가 넣어야 하는 비밀값: 값 없이 이름만
        else:
            raise AgentError("compose_invalid", f"알 수 없는 환경변수 값 조각: {p!r}")
    return '"' + "".join(out) + '"'


def _str_list(items) -> str:
    return "[" + ", ".join(_scalar([str(x)]) for x in items) + "]"


def _command(text: str) -> str:
    try:
        return _str_list(shlex.split(text))
    except ValueError as e:
        raise AgentError("compose_invalid", f"실행 명령을 나눌 수 없습니다: {text[:80]}") from e


def render(units: dict) -> tuple[str, list[str]]:
    """deploy_units → (compose.yaml.tftpl 내용, 사용자가 넣어야 하는 비밀값 이름들)."""
    containers, stores = units.get("containers") or [], units.get("datastores") or []
    image_ids = {i["id"] for i in units.get("images") or []}
    entry = units.get("entry") or {}
    ids = [s["id"] for s in [*containers, *stores]]
    if not containers or len(set(ids)) != len(ids) or not all(_ID.match(str(i)) for i in ids):
        raise AgentError("compose_invalid", "deploy_units 의 컨테이너 id 가 비었거나 형식이 맞지 않습니다")
    if entry.get("container") not in {c["id"] for c in containers} or not isinstance(entry.get("port"), int):
        raise AgentError("compose_invalid", "entry 컨테이너·포트가 없습니다")

    secrets: list[str] = []
    lines = [
        "# Pawploy 가 deploy_units 로 만든 compose (AgentCore 코드가 렌더, 직접 고치지 않음)",
        "# 외부 접속은 entry 컨테이너 하나만 80번으로. DB 비밀번호는 모듈이 배포 때 무작위로 만든다",
        f"name: {PROJECT_NAME}",
        "",
        "services:",
    ]
    for svc, is_store in [*((c, False) for c in containers), *((d, True) for d in stores)]:
        sid = svc["id"]
        run = svc.get("run") or {}
        lines.append(f"  {sid}:")
        # 컨테이너의 image 는 이미지 id, 데이터 저장소는 build_image 가 이미지 id 이고 image 는 레지스트리 이미지
        iid = svc.get("build_image") if is_store else svc.get("image")
        registry = svc.get("image") if is_store else svc.get("registry_image")
        if iid:
            if iid not in image_ids or not _ID.match(iid):
                raise AgentError("compose_invalid", f"{sid} 의 이미지 {iid!r} 가 images 에 없습니다")
            lines.append(f'    image: "{image_ref(iid)}"')
        elif registry:
            lines.append(f"    image: {_scalar([registry])}")
        else:
            raise AgentError("compose_invalid", f"{sid} 를 실행할 이미지가 없습니다")
        lines.append('    restart: "no"' if svc.get("one_shot") else "    restart: unless-stopped")
        if svc.get("entrypoint"):
            lines.append(f"    entrypoint: {_command(svc['entrypoint'])}")
        if svc.get("command"):
            lines.append(f"    command: {_command(svc['command'])}")
        if sid == entry["container"]:
            lines.append(f'    ports: ["{ENTRY_HOST_PORT}:{entry["port"]}"]')
        env = run.get("env") or {k: [v] for k, v in (svc.get("env") or {}).items()}
        if env:
            lines.append("    environment:")
            for name in sorted(env):
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    raise AgentError("compose_invalid", f"환경변수 이름 형식 오류: {name!r}")
                parts = env[name]
                line = f"      {name}: {_scalar(parts)}"
                if any(isinstance(p, dict) and "secret" in p for p in parts):
                    secrets.append(name)
                    line += "  # 사용자가 넣어야 하는 비밀값 (required_secrets)"
                lines.append(line)
        deps = run.get("depends_on") or {d: "service_started" for d in svc.get("depends_on") or []}
        if deps:
            lines.append("    depends_on:")
            for dep, cond in deps.items():
                if dep not in ids or cond not in ("service_started", "service_healthy", "service_completed_successfully"):
                    raise AgentError("compose_invalid", f"{sid}.depends_on 형식 오류: {dep} / {cond}")
                lines += [f"      {dep}:", f"        condition: {cond}"]
        hc = run.get("healthcheck")
        if hc:
            test = hc["test"]
            lines += ["    healthcheck:", f"      test: {_str_list(test) if isinstance(test, list) else _scalar([test])}"]
            for key in ("interval", "timeout", "start_period", "start_interval"):
                if hc.get(key):
                    lines.append(f"      {key}: {_scalar([hc[key]])}")
            if isinstance(hc.get("retries"), int):
                lines.append(f"      retries: {int(hc['retries'])}")
        if run.get("volumes"):
            lines.append("    volumes:")
            lines += [f"      - {_scalar([v])}" for v in run["volumes"]]
        lines.append("")
    if units.get("volumes"):
        lines.append("volumes:")
        for name in units["volumes"]:
            if not _ID.match(name):
                raise AgentError("compose_invalid", f"volume 이름 형식 오류: {name!r}")
            lines.append(f"  {name}: {{}}")
    text = "\n".join(lines).rstrip("\n") + "\n"
    errors = check(text, image_ids)
    if errors:
        raise AgentError("compose_invalid", "; ".join(errors))
    return text, sorted(set(secrets))


def check(text: str, image_ids: set[str] | None = None) -> list[str]:
    """Worker iac.check_template(+ check_template_images) 와 같은 검사 + 템플릿 값은 images·passwords 만."""
    e = [f"{TEMPLATE_NAME}: 템플릿 안에서 {f}() 로 파일을 읽을 수 없음" for f in TEMPLATE_FILE_FUNC_RE.findall(text)]
    e += [f"{TEMPLATE_NAME}: 허용하지 않는 compose 설정: {why}" for pat, why in COMPOSE_FORBIDDEN if re.search(pat, text)]
    unescaped = re.sub(r"\$\$\{|%%\{", "", text)
    allowed = set(TEMPLATE_VARS.values())
    for m in re.finditer(r"[$%]\{", unescaped):
        tail = unescaped[m.start():]
        im = _INTERP.match(tail)
        if not im or im.group(1) not in allowed:
            e.append(f"{TEMPLATE_NAME}: 허용하지 않는 템플릿 문법: {tail[:40]!r} (images·passwords 만)")
    if image_ids is not None:
        e += [f'{TEMPLATE_NAME}: images["{i}"] 가 이미지 목록에 없음' for i in sorted(set(IMAGE_REF_RE.findall(text)) - image_ids)]
    return e


def preview(text: str, images: dict[str, str], passwords: dict[str, str]) -> str:
    """Terraform templatefile 과 같은 방식으로 값을 채운 compose (로컬 실행·테스트용)."""
    values = {TEMPLATE_VARS["images"]: images, TEMPLATE_VARS["passwords"]: passwords}
    out, i = [], 0
    while i < len(text):
        if text.startswith("$${", i) or text.startswith("%%{", i):
            out.append(text[i] + "{")
            i += 3
        elif text.startswith("${", i):
            m = _INTERP.match(text, i)
            if not m or m.group(1) not in values or m.group(2) not in values[m.group(1)]:
                raise AgentError("compose_invalid", f"채울 수 없는 템플릿 값: {text[i:i + 40]!r}")
            out.append(values[m.group(1)][m.group(2)])
            i = m.end()
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def password_ids(text: str) -> list[str]:
    """모듈이 random_password 를 만들 id (Worker main.tf 의 local.password_ids 와 같은 규칙)."""
    return sorted(set(re.findall(r'passwords\["([A-Za-z0-9_.-]+)"\]', text)))
