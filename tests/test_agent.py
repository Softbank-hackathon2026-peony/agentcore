"""오프라인 테스트 (모델·AWS 호출 없음): python -m pytest -q"""
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from agent import buildfiles, source
from agent.errors import AgentError
from agent.handler import handle
from agent.scan import scan
from agent.schemas import DockerfileFix
from agent.storage import LocalStore
from tests.fakes import FakeBrain, recommendation

FIX = Path(__file__).parent / "fixtures"
SAMPLE = str(FIX / "sample_app")
MULTI = str(FIX / "multi_service")


def analyze(tmp_path, brain=None, **extra):
    store = LocalStore(str(tmp_path))
    payload = {"mode": "analyze", "project_id": "prj_demo", "source_uri": SAMPLE, "commit_sha": "abc123", **extra}
    return handle(payload, brain=brain or FakeBrain(), store=store), store


# ---------- 소스 읽기 ----------

def test_path_traversal_blocked():
    assert source.normalize("../etc/passwd") is None
    assert source.normalize("/etc/passwd") is None
    assert source.normalize("a/../../b") is None
    assert source.normalize("C:\\x") is None
    assert source.normalize("app/main.py") == "app/main.py"


def test_zip_slip_and_secret_and_root_strip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("repo-abc/app.py", "print(1)")
        z.writestr("repo-abc/.env", "OPENAI_API_KEY=sk-secret")
        z.writestr("../evil.sh", "rm -rf /")
    tree = source.SourceTree(source._unpack("x.zip", buf.getvalue()))
    assert tree.paths() == [".env", "app.py"]                 # 껍데기 폴더 제거, ../ 항목 버림
    with pytest.raises(AgentError) as e:
        tree.read_text(".env")
    assert e.value.code == "secret_file"


def test_tar_and_skip_dirs():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in {"app/index.js": b"x", "app/node_modules/a/b.js": b"y"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    tree = source.SourceTree(source._unpack("s.tar.gz", buf.getvalue()))
    assert tree.paths() == ["index.js"]


# ---------- 스캔 ----------

def test_scan_sample_app():
    s = scan(source.load(SAMPLE))
    assert s["languages"].get("python") == 1
    assert s["dockerfiles"] == ["Dockerfile"]
    assert 8080 in [p["port"] for p in s["port_hints"]]
    assert "PORT" in s["env_names"]
    assert s["warnings"] == []


def test_scan_multi_service_warns():
    s = scan(source.load(MULTI))
    assert s["compose_services"] == ["api", "worker", "db", "redis"]
    assert "fastapi" in s["frameworks"] and "postgres" in s["datastores"]
    assert {"DATABASE_URL", "REDIS_URL"} <= set(s["env_names"])
    assert any("서비스가 4개" in w for w in s["warnings"])


# ---------- analyze ----------

def test_analyze_ok_and_worker_fields(tmp_path):
    out, store = analyze(tmp_path)
    assert out["status"] == "ok", out
    rec = out["recommendation"]
    # Terraform Worker가 읽는 다섯 필드
    assert (rec["architecture"], rec["container_port"], rec["size"], rec["health_path"]) == ("lambda", 8080, "small", "/")
    assert rec["env"] == {"APP_MODE": "test"}                  # 비밀·예약 변수 제거
    assert "OPENAI_API_KEY" in rec["required_secrets"]
    assert [c["file"] for c in rec["clues"]] == ["app.py", "app.py"]   # 없는 파일 근거 제거
    assert rec["clues"][1]["line"] is None                     # 범위 밖 줄 번호 제거
    assert rec["candidates"][0]["target"] == "aws_lambda" and rec["candidates"][0]["rank"] == 1
    assert len(rec["candidates"]) == 5                         # 1~5순위
    assert rec["cost"]["monthly"] is not None and rec["cost"]["source"]   # 단가는 출처와 함께
    # 저장 경로
    saved = json.loads((tmp_path / f"projects/prj_demo/analysis/{out['analysis_id']}/recommendation.json").read_text("utf-8"))
    assert saved["architecture"] == "lambda"
    bdir = tmp_path / f"projects/prj_demo/build/{out['analysis_id']}/attempt-1"
    assert {p.name for p in bdir.iterdir()} == {"Dockerfile.pawploy", "dockerignore", "buildspec.yml"}


def test_analyze_forces_dockerfile_rules(tmp_path):
    out, _ = analyze(tmp_path)
    df = out["build_files"]["dockerfile"]
    assert buildfiles.LWA_LINE in df and "ENV PORT=8080" in df and "EXPOSE 8080" in df
    assert df.index("EXPOSE 8080") < df.index("CMD")


def test_analyze_non_deployable_target_falls_back(tmp_path):
    brain = FakeBrain(rec=recommendation(target="aws_ecs_fargate"))
    out, _ = analyze(tmp_path, brain=brain)
    assert out["recommendation"]["target"] == "aws_lambda"
    assert any("배포 불가" in n for n in out["validation_notes"])


def test_analyze_revision_passed_to_brain(tmp_path):
    brain = FakeBrain()
    analyze(tmp_path, brain=brain, revision_message="항상 켜져 있어야 해", previous_recommendation={"target": "aws_lambda"})
    assert brain.calls[0][1]["message"] == "항상 켜져 있어야 해"


def test_analyze_multi_service_unsupported(tmp_path):
    out, _ = analyze(tmp_path, source_uri=MULTI)
    assert out["recommendation"]["supported"] is False


def test_unsafe_dockerfile_rejected(tmp_path):
    from agent.schemas import DockerfileOut
    bad = DockerfileOut(dockerfile="FROM node:20\nCOPY .env /app/.env\nCMD node x", container_port=3000)
    out, _ = analyze(tmp_path, brain=FakeBrain(df=bad))
    assert out["status"] == "error" and out["error"]["code"] == "dockerfile_unsafe"


def test_bad_requests():
    assert handle({"mode": "nope"}, brain=FakeBrain())["error"]["code"] == "unknown_mode"
    assert handle({"mode": "analyze"}, brain=FakeBrain())["error"]["code"] == "bad_request"
    assert handle({"mode": "analyze", "project_id": "p", "source_uri": "Z:/none"},
                  brain=FakeBrain())["error"]["code"] == "source_not_found"


# ---------- fix_build ----------

def fix_payload(**over):
    return {"mode": "fix_build", "project_id": "prj_demo", "analysis_id": "ana-1", "source_uri": SAMPLE,
            "dockerfile": "FROM python:3.12-slim\nCMD python app.py\n", "build_log": "ERROR: ...",
            "failed_phase": "BUILD", "attempt": 1, **over}


def test_fix_build_ok(tmp_path):
    out = handle(fix_payload(), brain=FakeBrain(), store=LocalStore(str(tmp_path)))
    assert out["status"] == "ok" and out["build_files"]["attempt"] == 2
    assert (tmp_path / "projects/prj_demo/build/ana-1/attempt-2/Dockerfile.pawploy").exists()


def test_fix_build_gives_up_after_max(tmp_path):
    out = handle(fix_payload(attempt=4), brain=FakeBrain(), store=LocalStore(str(tmp_path)))
    assert out["status"] == "give_up"


def test_fix_build_not_fixable(tmp_path):
    brain = FakeBrain(fix=DockerfileFix(fixable=False, cause="ECR 로그인 권한 없음"))
    out = handle(fix_payload(), brain=brain, store=LocalStore(str(tmp_path)))
    assert out["status"] == "give_up" and out["fixable"] is False


def test_cost_missing_price_is_null(monkeypatch):
    from agent import cost
    monkeypatch.setitem(cost._PRICES, "aws_ec2", {"small": {"hourly": None}})
    e = cost.estimate("aws_ec2", "small")
    assert e["monthly"] is None and e["note"] == "단가 확인 전"   # 단가 없으면 숫자를 지어내지 않음


def test_buildspec_is_fixed_template():
    spec = buildfiles.buildspec()
    assert "docker build --platform linux/amd64" in spec and "GCP_AR_REPO" in spec
    assert "exported-variables" in spec
