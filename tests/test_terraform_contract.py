"""Worker(Terraform-worker) 모듈 규격과 맞는지: tfworker/iac.py · policy.py 와 같은 규칙을 지키는지 확인한다.

WORKER_REPO=<Terraform-worker 경로> 를 주면 Worker 의 iac.check_code 를 직접 불러
같은 입력에 대해 Worker 가 거부하는 것은 우리도 거부하는지(우리가 더 엄격한 건 괜찮음) 비교한다.
"""
import os
import sys
from pathlib import Path

import pytest

from agent import terraform as tf
from agent.schemas import TfFile
from tests.fakes import reference_files

ARCHS = ["ec2", "lambda", "cloud_run", "ec2_compose"]


def _with(arch: str, extra: str = "", replace: tuple[str, str] | None = None) -> list[TfFile]:
    files = reference_files(arch)
    main = files[0].content
    if replace:
        assert replace[0] in main, replace[0]
        main = main.replace(*replace)
    return [TfFile(name="main.tf", content=main + "\n" + extra)] + files[1:]


def _errors(files, arch):
    return tf.check_files(files, arch)[1]


# (아키텍처, 바꿀 내용, 오류 메시지에 들어 있어야 할 말)
VIOLATIONS = [
    ("lambda", {"extra": 'terraform {\n  cloud {\n    organization = "x"\n  }\n}'}, "cloud 블록"),
    ("ec2", {"extra": 'locals { t = { default_tags = { a = "b" } } }'}, "default_tags"),
    ("cloud_run", {"extra": "locals { default_labels = {} }"}, "default_labels"),
    ("ec2", {"extra": "# default_tags 는 루트가 넣는다"}, "default_tags"),     # Worker 는 주석도 검사
    ("lambda", {"extra": 'data "aws_ssm_parameter" "db" {\n  name = "/pawploy/db_password"\n}'}, "/aws/service/"),
    ("ec2", {"replace": ('cpu_credits = "standard"', 'cpu_credits = "unlimited"')}, "cpu_credits"),
    ("cloud_run", {"replace": ("deletion_protection = false", "deletion_protection = true")}, "deletion_protection"),
    ("lambda", {"extra": 'data "aws_caller_identity" "me" {}'}, "data 소스"),
    ("cloud_run", {"extra": 'data "aws_region" "r" {}'}, "data 소스"),          # 클라우드별 data 목록
    ("cloud_run", {"extra": 'resource "google_project_iam_member" "x" {}'}, "google_project_iam_member"),
    ("ec2", {"extra": 'resource "google_service_account" "x" {}'}, "google_service_account"),
    ("lambda", {"extra": 'resource "aws_iam_role_policy_attachment" "admin" {\n'
                         '  role       = "x"\n  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"\n}'},
     "AdministratorAccess"),
    ("lambda", {"extra": 'locals { p = { inline_policy = "x" } }'}, "인라인 정책"),
    ("cloud_run", {"extra": 'locals { t = data.google_client_config.current.access_token }'}, "access_token"),
    ("lambda", {"extra": 'terraform {\n  required_providers {\n    aws = { source = "evil/aws" }\n  }\n}'},
     "provider source"),
    ("cloud_run", {"replace": ("max_instance_count = 1", "max_instance_count = 5")}, "최대 인스턴스"),
    ("ec2_compose", {"replace": ('cpu_credits = "standard"', 'cpu_credits = "unlimited"')}, "cpu_credits"),
    ("ec2_compose", {"extra": 'variable "image_uri" { type = string }'}, "넘겨주지 않는 변수"),
    ("ec2", {"extra": 'resource "random_password" "x" {\n  length = 8\n}'}, "random_password"),   # ec2_compose 만
    ("ec2_compose", {"replace": ("role       = aws_iam_role.app.name", 'role       = "admin"')}, "문자열"),
]


# ---------- ec2_compose 템플릿 (compose.yaml.tftpl) ----------

def _compose_with(extra: str):
    files = reference_files("ec2_compose")
    files[2] = TfFile(name="compose.yaml.tftpl", content=files[2].content + extra)
    return files


TEMPLATE_VIOLATIONS = [
    ("    privileged: true\n", "privileged"),
    ("    network_mode: host\n", "host"),
    ('    volumes: ["/var/run/docker.sock:/var/run/docker.sock"]\n', "Docker 소켓"),
    ("    cap_add: [NET_ADMIN]\n", "cap_add"),
    ("    build: .\n", "build"),
    ('# ${file("/etc/passwd")}\n', "파일을 읽을 수 없음"),
]


@pytest.mark.parametrize("extra, needle", TEMPLATE_VIOLATIONS)
def test_compose_template_violations(extra, needle):
    errors = _errors(_compose_with(extra), "ec2_compose")
    assert any(needle in e for e in errors), errors


def test_compose_module_needs_template():
    assert any("compose.yaml.tftpl" in e for e in _errors(reference_files("ec2_compose")[:2], "ec2_compose"))


@pytest.mark.parametrize("arch", ARCHS)
def test_reference_is_worker_module(arch):
    assert _errors(reference_files(arch), arch) == []


def test_cloud_run_reference_has_app_service_account():
    main = reference_files("cloud_run")[0].content
    assert 'resource "google_service_account" "app"' in main
    assert "service_account = google_service_account.app.email" in main


def test_aws_public_ssm_parameter_allowed():
    extra = 'data "aws_ssm_parameter" "al2023" {\n  name = "/aws/service/ami-amazon-linux-latest/x"\n}'
    assert _errors(_with("lambda", extra), "lambda") == []


@pytest.mark.parametrize("arch, change, needle", VIOLATIONS)
def test_worker_contract_violations(arch, change, needle):
    errors = _errors(_with(arch, **change), arch)
    assert any(needle in e for e in errors), errors


def test_allowed_resources_match_worker_policy():
    assert tf.ALLOWED_RESOURCES["cloud_run"] == {
        "google_service_account", "google_cloud_run_v2_service", "google_cloud_run_v2_service_iam_member"}
    assert set(tf.ALLOWED_RESOURCES) == set(tf.CLOUD_OF)


# ---------- Worker 코드와 직접 비교 (WORKER_REPO 가 있을 때만) ----------

def _worker():
    repo = os.environ.get("WORKER_REPO")
    if not repo:
        pytest.skip("WORKER_REPO 미지정")
    sys.path.insert(0, repo)
    try:
        from tfworker import iac, policy
    finally:
        sys.path.remove(repo)
    return iac, policy


def test_constants_equal_worker():
    iac, policy = _worker()
    assert tf.ALLOWED_RESOURCES == policy.ALLOWED_TYPES
    assert tf.ALLOWED_DATA == iac.ALLOWED_DATA
    assert tf.ALLOWED_POLICY_ARNS == policy.ALLOWED_POLICY_ARNS
    assert tf.PROVIDER_SOURCES == iac.PROVIDER_SOURCES
    assert tf.ARCHITECTURE_PROVIDER_SOURCES == iac.ARCHITECTURE_PROVIDER_SOURCES
    assert [p for p, _ in tf.FORBIDDEN] == [p for p, _ in iac.FORBIDDEN]
    assert tf.REQUIRED_VARS == iac.CONTRACT_VARIABLES and tf.REQUIRED_OUTPUTS == iac.CONTRACT_OUTPUTS
    assert tf.COMPOSE_REQUIRED_VARS == iac.COMPOSE_CONTRACT_VARIABLES
    assert tf.ALLOWED_INSTANCE_TYPES == policy.ALLOWED_INSTANCE_TYPES
    assert (tf.MAX_LAMBDA_MEMORY_MB, tf.MAX_LAMBDA_TIMEOUT_SEC) == (policy.MAX_LAMBDA_MEMORY_MB,
                                                                     policy.MAX_LAMBDA_TIMEOUT_SEC)
    assert (tf.MAX_CLOUD_RUN_MEMORY_MI, tf.MAX_CLOUD_RUN_INSTANCES) == (policy.MAX_CLOUD_RUN_MEMORY_MI,
                                                                         policy.MAX_CLOUD_RUN_INSTANCES)


def test_compose_contract_equals_worker():
    """compose.yaml.tftpl 약속: 템플릿 검사·이미지 id 규칙·템플릿 이름이 Worker 와 같은지."""
    iac, _ = _worker()
    from tfworker import job, render
    from agent import compose
    assert [p for p, _ in compose.COMPOSE_FORBIDDEN] == [p for p, _ in iac.COMPOSE_FORBIDDEN]
    assert compose.TEMPLATE_FILE_FUNC_RE.pattern == iac.TEMPLATE_FILE_FUNC_RE.pattern
    assert compose.IMAGE_REF_RE.pattern == iac.IMAGE_REF_RE.pattern
    assert compose.TEMPLATE_NAME == render.COMPOSE_TEMPLATE
    from agent import units
    assert units.ID_RE.pattern == job.IMAGE_ID_RE.pattern
    main = Path(os.environ["WORKER_REPO"], "modules", "ec2_compose", "main.tf").read_text(encoding="utf-8")
    import re
    args = main.split('templatefile("${path.module}/compose.yaml.tftpl"')[1].split("})")[0]
    for var in compose.TEMPLATE_VARS.values():          # 모듈이 템플릿에 넘기는 값 이름
        assert re.search(rf"\b{var}\s*=", args), var


@pytest.mark.parametrize("fixture", ["compose_board", "compose_vote", "multi_service"])
def test_worker_accepts_rendered_compose_module(fixture, tmp_path):
    """fixture 를 analyze → gen_terraform 한 ec2_compose 모듈 폴더를 Worker iac.find_violations 에 그대로 넣는다."""
    iac, _ = _worker()
    from agent.handler import handle
    from agent.storage import LocalStore
    from tests.fakes import FakeBrain, recommendation
    store = LocalStore(str(tmp_path))
    rec = handle({"mode": "analyze", "project_id": "prj", "source_uri": str(Path(__file__).parent / "fixtures" / fixture)},
                 brain=FakeBrain(rec=recommendation(target="aws_ec2_compose", env={})), store=store)["recommendation"]
    gen = handle({"mode": "gen_terraform", "project_id": "prj", "deploy_id": "dep-1", "recommendation": rec},
                 brain=FakeBrain(), store=store)
    target = gen["targets"][0]
    assert target["status"] == "ok", target
    module = Path(target["module_uri"])
    images = {i: f"123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/x@sha256:{'0' * 64}" for i in target["images"]}
    assert iac.find_violations(module, "aws", "ec2_compose", images) == []
    from tfworker import render
    assert render.detect_architecture(module) == "ec2_compose"


@pytest.mark.parametrize("arch", ARCHS)
def test_reference_equals_worker_module(arch):
    _worker()
    from pathlib import Path
    for f in reference_files(arch):
        if f.name == "compose.yaml.tftpl":            # 앱마다 코드가 렌더 (Worker 것은 예시)
            continue
        name = "main.tf" if f.name.endswith(".tf") else f.name
        worker = Path(os.environ["WORKER_REPO"], "modules", arch, name).read_text(encoding="utf-8")
        assert f.content.strip() == worker.replace("\r\n", "\n").strip(), f"{arch}/{name} 가 Worker 모듈과 다름"


@pytest.mark.parametrize("arch, change, needle", VIOLATIONS)
def test_we_reject_whatever_worker_rejects(arch, change, needle):
    iac, _ = _worker()
    files = _with(arch, **change)
    worker_errors = iac.check_code(files[0].content, tf.CLOUD_OF[arch], arch)
    ours = _errors(files, arch)
    assert not worker_errors or ours, (worker_errors, ours)


@pytest.mark.parametrize("arch", ARCHS)
def test_worker_accepts_reference(arch):
    iac, _ = _worker()
    assert iac.check_code(reference_files(arch)[0].content, tf.CLOUD_OF[arch], arch) == []
