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
    assert [w for w in s["warnings"] if not w.startswith("InfraFit: 추천 대상")] == []


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


# ---------- gen_terraform / fix_terraform ----------

from agent import terraform as tf  # noqa: E402
from agent.schemas import TerraformFix, TerraformOut, TfFile  # noqa: E402
from tests.fakes import reference_files  # noqa: E402


@pytest.mark.parametrize("arch", ["ec2", "lambda", "cloud_run"])
def test_reference_modules_pass_own_checks(arch):
    files, errors, _ = tf.check_files(reference_files(arch), arch)
    assert errors == [], errors


def _main(extra: str, arch="lambda"):
    base = reference_files(arch)[0].content
    return [TfFile(name="main.tf", content=base + "\n" + extra)]


@pytest.mark.parametrize("extra, needle", [
    ('resource "aws_s3_bucket" "x" {}', "aws_s3_bucket"),
    ('resource "null_resource" "x" {\n  provisioner "local-exec" { command = "curl x" }\n}', "provisioner"),
    ('terraform {\n  backend "s3" {}\n}', "backend"),
    ('variable "secret" { type = string }', "넘겨주지 않는 변수"),
    ('data "external" "x" { program = ["sh"] }', "data 소스"),
    ('locals { k = file("/etc/passwd") }', "파일 읽기"),
    ('module "m" { source = "git::https://evil" }', "module"),
])
def test_terraform_violations(extra, needle):
    _, errors, _ = tf.check_files(_main(extra), "lambda")
    assert any(needle in e for e in errors), errors


def test_terraform_limits():
    _, errors, _ = tf.check_files(_main('locals { big = "m5.4xlarge" }', "ec2") + reference_files("ec2")[1:], "ec2")
    assert any("인스턴스 타입" in e for e in errors)
    _, errors, _ = tf.check_files(_main("locals { x = { memory_size = 10240 } }"), "lambda")
    assert any("메모리" in e for e in errors)


def test_provider_block_removed():
    files, errors, fixes = tf.check_files(_main('provider "aws" {\n  region = "us-east-1"\n}'), "lambda")
    assert errors == [] and "provider \"aws\"" not in files[0].content and fixes


def test_missing_output():
    content = reference_files("lambda")[0].content.replace('output "resource_id"', 'output "rid"')
    _, errors, _ = tf.check_files([TfFile(name="main.tf", content=content)], "lambda")
    assert any("resource_id" in e for e in errors)


def tf_payload(**over):
    return {"mode": "gen_terraform", "project_id": "prj_demo", "deploy_id": "dep-1",
            "recommendation": {"cloud": "aws", "architecture": "lambda", "container_port": 8080, "size": "small",
                               "health_path": "/", "env": {"APP_MODE": "test"}}, **over}


def test_gen_terraform_makes_aws_and_gcp(tmp_path):
    brain = FakeBrain()
    out = handle(tf_payload(), brain=brain, store=LocalStore(str(tmp_path)))
    assert out["status"] == "ok", out
    got = {t["cloud"]: t["architecture"] for t in out["targets"]}
    assert got == {"aws": "lambda", "gcp": "cloud_run"}
    base = tmp_path / "projects/prj_demo/deploy/dep-1/attempt-1"
    assert (base / "aws/main.tf").exists() and (base / "gcp/main.tf").exists()
    assert out["module_uri"].endswith("attempt-1/")
    assert all(t["module_uri"].endswith(f"attempt-1/{t['cloud']}/") for t in out["targets"])


def test_gen_terraform_other_cloud_from_candidates(tmp_path):
    rec = {"cloud": "gcp", "architecture": "cloud_run", "container_port": 8080, "size": "small",
           "health_path": "/", "env": {}, "candidates": [
               {"rank": 1, "cloud": "gcp", "architecture": "cloud_run", "deployable": True},
               {"rank": 2, "cloud": "aws", "architecture": "ecs_fargate", "deployable": False},
               {"rank": 3, "cloud": "aws", "architecture": "ec2", "deployable": True},
               {"rank": 4, "cloud": "aws", "architecture": "lambda", "deployable": True}]}
    assert tf.plan_targets(rec) == [("aws", "ec2"), ("gcp", "cloud_run")]
    assert tf.plan_targets(rec, {"aws": "lambda"}) == [("aws", "lambda")]
    with pytest.raises(Exception):
        tf.plan_targets(rec, {"gcp": "ec2"})


def test_gen_terraform_retries_after_violation(tmp_path):
    bad = TerraformOut(files=_main('resource "aws_s3_bucket" "x" {}'))
    brain = FakeBrain(tf=[bad])            # 첫 번째는 위반, 두 번째는 견본
    out = handle(tf_payload(architectures={"aws": "lambda"}), brain=brain, store=LocalStore(str(tmp_path)))
    assert out["status"] == "ok" and len(out["targets"]) == 1
    assert brain.calls[1][2] and "aws_s3_bucket" in brain.calls[1][2][0]   # 위반 내용을 다시 넘김


def test_gen_terraform_partial(tmp_path):
    class HalfBad(FakeBrain):
        def gen_terraform(self, ctx, arch, reference, errors):
            if arch == "cloud_run":
                return TerraformOut(files=_main('resource "aws_s3_bucket" "x" {}', "cloud_run"))
            return super().gen_terraform(ctx, arch, reference, errors)
    out = handle(tf_payload(), brain=HalfBad(), store=LocalStore(str(tmp_path)))
    assert out["status"] == "partial"
    st = {t["cloud"]: t["status"] for t in out["targets"]}
    assert st == {"aws": "ok", "gcp": "error"}
    assert not (tmp_path / "projects/prj_demo/deploy/dep-1/attempt-1/gcp").exists()


def test_gen_terraform_unsupported_arch(tmp_path):
    p = tf_payload(recommendation={"architecture": "ecs_fargate"})
    assert handle(p, brain=FakeBrain(), store=LocalStore(str(tmp_path)))["error"]["code"] == "unsupported_architecture"


def fix_tf_payload(**over):
    return {"mode": "fix_terraform", "project_id": "prj_demo", "deploy_id": "dep-1", "architecture": "lambda",
            "attempt": 1, "failed_stage": "apply", "log": "Error: ...",
            "files": {f.name: f.content for f in reference_files("lambda")}, **over}


def test_fix_terraform_ok_and_give_up(tmp_path):
    out = handle(fix_tf_payload(), brain=FakeBrain(), store=LocalStore(str(tmp_path)))
    assert out["status"] == "ok" and out["next_attempt"] == 2 and out["saved_attempt"] == 2
    assert (tmp_path / "projects/prj_demo/deploy/dep-1/attempt-2/aws/main.tf").exists()
    assert handle(fix_tf_payload(attempt=4), brain=FakeBrain())["status"] == "give_up"
    nf = FakeBrain(tf_fix=TerraformFix(fixable=False, cause="서비스 할당량 초과"))
    assert handle(fix_tf_payload(), brain=nf, store=LocalStore(str(tmp_path)))["fixable"] is False


def test_fix_terraform_keeps_other_cloud_in_new_attempt(tmp_path):
    """Worker 는 가장 큰 attempt-N 하나만 본다 → AWS 만 고쳐도 GCP 모듈이 새 attempt 에 같이 있어야 한다."""
    store = LocalStore(str(tmp_path))
    handle(tf_payload(), brain=FakeBrain(), store=store)
    root = tmp_path / "projects/prj_demo/deploy/dep-1"
    p = fix_tf_payload()
    del p["files"]                           # 저장된 최신 attempt 에서 읽는다
    out = handle(p, brain=FakeBrain(), store=store)
    assert out["status"] == "ok" and out["saved_attempt"] == 2 and out["carried_over"] == ["gcp"]
    assert "# fixed" in (root / "attempt-2/aws/main.tf").read_text(encoding="utf-8")
    assert (root / "attempt-2/gcp/main.tf").read_text(encoding="utf-8") == \
        (root / "attempt-1/gcp/main.tf").read_text(encoding="utf-8")

    # 이어서 GCP 가 실패 (Main Server 기준 GCP 는 1번째 시도) → attempt-3, AWS 는 attempt-2 의 고친 것을 유지
    g = fix_tf_payload(architecture="cloud_run")
    del g["files"]
    out = handle(g, brain=FakeBrain(), store=store)
    assert out["saved_attempt"] == 3 and out["next_attempt"] == 2
    assert "# fixed" in (root / "attempt-3/gcp/main.tf").read_text(encoding="utf-8")
    assert "# fixed" in (root / "attempt-3/aws/main.tf").read_text(encoding="utf-8")


def test_fix_terraform_from_attempt_folder_uri(tmp_path):
    store = LocalStore(str(tmp_path))
    gen = handle(tf_payload(), brain=FakeBrain(), store=store)
    p = fix_tf_payload(architecture="cloud_run", module_uri=gen["module_uri"])
    del p["files"]
    out = handle(p, brain=FakeBrain(), store=store)
    assert out["status"] == "ok" and "google_cloud_run_v2_service" in out["files"]["main.tf"]


# ---------- InfraFit 인벤토리 ----------

from agent import inventory  # noqa: E402


def test_inventory_sample_app():
    inv = scan(source.load(SAMPLE))["inventory"]
    assert inv["status"] == "ok", inv
    assert len(inv["infrafit_commit"]) == 40
    s = inv["summary"]
    assert [w["kind"] for w in s["workloads"]] == ["web"]
    assert s["workloads"][0]["at"] and source.load(SAMPLE).exists(s["workloads"][0]["at"].split(":")[0])


def test_inventory_multi_service():
    s = scan(source.load(MULTI))
    inv = s["inventory"]
    assert inv["status"] == "ok", inv
    assert {w["name"]: w["kind"] for w in inv["summary"]["workloads"]} == {"api": "web", "worker": "worker"}
    assert {d["role"] for d in inv["summary"]["datastores"]} == {"primary-db", "cache"}
    assert any("앱 워크로드가 2개" in w for w in s["warnings"])
    tree = source.load(MULTI)
    assert inv["summary"]["endpoints"]["total"] >= 1
    for line in inv["summary"]["endpoints"]["first"]:          # 근거는 실제 파일·줄
        path, _, ln = line.split(" @", 1)[1].split(" ")[0].partition(":")
        assert tree.exists(path) and 1 <= int(ln) <= len(tree.lines(path))
    assert any("서비스가 4개" in w for w in s["warnings"])          # 기존 경고 유지


def test_inventory_secrets_not_written_and_env_example_names_only(tmp_path):
    tree = source.SourceTree({"app.py": b"import os\n", ".env": b"OPENAI_API_KEY=sk-real",
                              ".env.example": b"# comment sk-x\nOPENAI_API_KEY=sk-example\n"})
    root = tmp_path / "repo"
    inventory._materialize(tree, root)
    assert not (root / ".env").exists()
    assert (root / ".env.example").read_text() == "\nOPENAI_API_KEY=\n"


def test_inventory_failure_does_not_break_analyze(tmp_path, monkeypatch):
    assert inventory.run_inventory(source.load(SAMPLE), timeout_s=0)["status"] == "timeout"
    monkeypatch.setattr(inventory, "VENDOR_DIR", tmp_path / "nope")
    bad = inventory.run_inventory(source.load(SAMPLE))
    assert bad["status"] == "error" and "InfraFit" in bad["message"]
    brain = FakeBrain()
    out, _ = analyze(tmp_path, brain=brain)
    assert out["status"] == "ok"


def test_inventory_disabled_by_config(monkeypatch):
    from agent import config
    monkeypatch.setattr(config, "INVENTORY_TIMEOUT", 0)
    assert scan(source.load(SAMPLE))["inventory"]["status"] == "skipped"


def test_inventory_summary_bounded():
    eps = [{"id": f"ep-{i}", "workload": f"w-{i % 40}", "method": "GET", "route": "/r" * 40 + str(i),
            "handler": {"path": f"src/routes/very/long/path/file_{i}.py", "line": i + 1, "snippet": "x"},
            "status": "confirmed", "exposure": [{"environment": "prod", "value": "routed"}]} for i in range(500)]
    inv = {"workloads": [{"id": f"w-{i}", "kind": "web", "name": f"svc-{i}" * 5, "status": "confirmed",
                          "entrypoint": {"path": f"services/{i}/main.py", "line": 1, "snippet": "x"}}
                         for i in range(40)],
           "endpoints": eps, "datastores": [], "external_services": [], "environments": [],
           "current_components": [], "request_paths": [],
           "unmapped": [{"label": "SIG-" + "X" * 200} for _ in range(50)]}
    s = inventory.summarize(inv)
    assert len(json.dumps(s, ensure_ascii=False).encode()) <= inventory.MAX_SUMMARY_BYTES
    assert s["endpoints"]["total"] == 500 and s["truncated"]


# ---------- InfraFit 추천 (S2~S4) ----------

def test_inventory_recommendation_fixtures():
    s = scan(source.load(SAMPLE))
    reco = s["inventory"]["summary"]["recommendation"]
    assert "stage_error" not in s["inventory"]
    assert reco["recommended"]["target"] == "aws_lambda" and reco["recommended"]["deployable"] is True
    assert reco["recommended"]["assignment"] == {"w-app": "cp:aws/lambda/function-url"}
    assert 1 <= len(reco["top"]) <= 5 and reco["top"][0] == reco["recommended"]
    assert set(reco["dimensions"]) == set(inventory.APP_DIMENSIONS)
    assert reco["dimensions"]["A2"]["assumed"] is True and reco["dimensions"]["A2"]["why"]
    assert any(w.startswith("InfraFit: 추천 대상 aws_lambda") for w in s["warnings"])

    m = scan(source.load(MULTI))
    reco = m["inventory"]["summary"]["recommendation"]
    rec = reco["recommended"]
    assert rec["target"] == "aws_ec2" and rec["assignment"]["w-app"] == "cp:aws/ec2/docker-compose"
    assert set(rec["assignment"]) == {"w-app", "ds-postgresql", "svc-redis"}
    assert rec["monthly_baseline_usd"] is None or rec["monthly_baseline_usd"] > 0
    lam = next(r for r in reco["rejected"] if r["target"] == "aws_lambda")
    why = lam["reasons"][0]
    assert why["rule"] == "CAP-ALWAYSON-001" and why["dimension"] == "A1"
    assert why["dimension_value"] == ["웹", "워커"] and why["capability_value"] is False
    assert why["source"]["url"].startswith("https://") and why["source"]["quote"]
    tree = source.load(MULTI)
    for at in reco["dimensions"]["A1"]["at"]:                  # 근거는 실제 파일·줄
        path, _, ln = at.partition(":")
        assert tree.exists(path) and 1 <= int(ln) <= len(tree.lines(path))
    warn = next(w for w in m["warnings"] if w.startswith("InfraFit: 추천 대상"))
    assert "aws_ec2" in warn and "탈락: aws_lambda(CAP-ALWAYSON-001" in warn


def _broken_vendor(tmp_path, stage_file: str, body: str):
    import shutil
    vendor = tmp_path / "infrafit"
    shutil.copytree(inventory.VENDOR_DIR, vendor, ignore=shutil.ignore_patterns("__pycache__"))
    f = vendor / "infrafit" / "stages" / stage_file
    f.write_text(f.read_text(encoding="utf-8") + body, encoding="utf-8")
    return vendor


def test_inventory_recommendation_failure_keeps_s1(tmp_path, monkeypatch):
    vendor = _broken_vendor(tmp_path, "s2_profile.py",
                            "\n\ndef run_s2(*a, **k):\n    raise RuntimeError('S2 고장')\n")
    monkeypatch.setattr(inventory, "VENDOR_DIR", vendor)
    s = scan(source.load(MULTI))
    inv = s["inventory"]
    assert inv["status"] == "ok" and inv["stage_error"]["stage"] == "S2"
    assert "S2 고장" in inv["stage_error"]["message"]
    assert "recommendation" not in inv["summary"]
    assert {w["name"] for w in inv["summary"]["workloads"]} == {"api", "worker"}
    assert any("추천 단계(S2)" in w for w in s["warnings"])
    out, _ = analyze(tmp_path / "store", brain=FakeBrain())
    assert out["status"] == "ok"


def test_inventory_recommendation_timeout_keeps_s1(tmp_path, monkeypatch):
    vendor = _broken_vendor(tmp_path, "s4_recommend.py",
                            "\n\ndef run_s4(*a, **k):\n    import time\n    time.sleep(60)\n")
    monkeypatch.setattr(inventory, "VENDOR_DIR", vendor)
    inv = inventory.run_inventory(source.load(SAMPLE), timeout_s=4)
    assert inv["status"] == "ok" and inv["stage_error"]["stage"] == "S4"
    assert "timeout" in inv["stage_error"]["message"]
    assert inv["summary"]["workloads"] and "recommendation" not in inv["summary"]


def test_inventory_summary_with_recommendation_bounded():
    eps = [{"id": f"ep-{i}", "workload": "w-a", "method": "GET", "route": "/r" * 40 + str(i),
            "handler": {"path": f"src/routes/file_{i}.py", "line": i + 1, "snippet": "x"}} for i in range(300)]
    inv = {"workloads": [{"id": "w-a", "kind": "web", "name": "a"}], "endpoints": eps}
    long = "Q" * 400
    comps = [f"cp:aws/x{i}/" + "y" * 40 for i in range(12)]
    reco = {"recommended": "C1",
            "candidates": [{"id": f"C{i + 1}", "rank": i + 1, "unknown_count": 0,
                            "assignment": {"w-app": c, **{f"ds-{j}": "ds:" + "z" * 60 for j in range(6)}},
                            "cost": {"monthly_baseline_usd": 1.0}} for i, c in enumerate(comps)],
            "rejected": [{"id": c, "reasons": [{"type": "violation", "detail": "d", "violation": {
                "rule": f"R-{k}", "dimension": "A1", "capability_key": "CP.k", "actual": False,
                "source": {"ref": "https://example.com/" + "p" * 80, "quote": long}}} for k in range(5)]}
                for c in comps]}
    profile = {"dimensions": [{"scope": "w-app", "dimension": d, "value": "v" * 50, "source": "assumption",
                               "assumption_key": d, "evidence": []} for d in inventory.APP_DIMENSIONS],
               "assumptions": [{"key": d, "reason": long} for d in inventory.APP_DIMENSIONS]}
    s = inventory.summarize(inv, profile, {"matrix": []}, reco)
    size = len(json.dumps(s, ensure_ascii=False).encode())
    assert size <= inventory.MAX_SUMMARY_BYTES, size
    r = s["recommendation"]
    assert r["recommended"]["id"] == "C1" and r["top"] and len(r["top"]) <= 5
    assert all(len(x["reasons"]) <= 3 for x in r["rejected"])


def test_inventory_outcome_detail_is_text():
    from agent import inventory
    reco = {"outcome": "static_only", "candidates": [], "rejected": [],
            "outcome_detail": {"message": "정적 사이트", "current": [{"component": "unmapped", "label": "static hosting (vercel.json)"}]}}
    block = inventory.recommendation_summary({"dimensions": []}, {"matrix": []}, reco)
    assert block["outcome"] == "static_only"
    assert block["outcome_detail"] == "정적 사이트 / 현재: static hosting (vercel.json)"
