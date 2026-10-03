"""Preserved Dockerfile selection and failure handling for parallel registry work."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from agent import analyze, buildfiles
from agent.brain import StrandsBrain
from agent.errors import AgentError
from agent.source import SourceTree
from agent.schemas import AnalysisOut, DockerfileOut
from agent.storage import LocalStore
from tests.fakes import FakeBrain, recommendation


TWEET = ('FROM nginx:latest\nCOPY index.html /usr/share/nginx/html\n'
         'COPY linux.png /usr/share/nginx/html\nEXPOSE 80 443\n'
         'CMD ["nginx", "-g", "daemon off;"]\n')


def test_existing_nginx_skips_dockerfile_model_and_keeps_runtime(monkeypatch):
    src = SourceTree({"Dockerfile": TWEET.encode(), "index.html": b"hello"})
    brain = StrandsBrain()
    calls = []
    monkeypatch.setattr(brain, "_agent", lambda src: object())
    def run(agent, prompt, model):
        calls.append(model)
        return AnalysisOut(**recommendation(container_port=8080).model_dump(),
                           dockerfile="FROM nginx\nRUN broken-template-rewrite\n")
    monkeypatch.setattr(brain, "_run", run)
    rec, df = brain.analyze(src, {"tree": src.paths()}, None)
    assert len(calls) == 1
    assert rec.container_port == df.container_port == 80
    assert df.dockerfile == TWEET
    normalized, _ = buildfiles.normalize_dockerfile(df.dockerfile, rec.container_port)
    assert 'CMD ["nginx", "-g", "daemon off;"]' in normalized
    assert "templates" not in normalized
    assert buildfiles.LWA_LINE in normalized


@pytest.mark.parametrize("body", [
    "FROM nginx\n", "FROM nginx\nEXPOSE $PORT\n", "FROM nginx\nEXPOSE 80/udp\n",
    "FROM nginx\nEXPOSE 3000 5000\n", "ARG BASE\nFROM $BASE\nEXPOSE 80\n",
    "FROM python\nEXPOSE 8000\nCMD uvicorn app:app --reload\n",
    "FROM nginx\nEXPOSE 65536\n",
])
def test_uncertain_existing_dockerfile_uses_model(body):
    assert buildfiles.existing_dockerfile(SourceTree({"Dockerfile": body.encode()}), 8080) is None


def test_existing_file_retains_secret_copy_guard():
    src = SourceTree({"Dockerfile": b"FROM nginx\nCOPY .env /app/.env\nEXPOSE 80\n"})
    df = buildfiles.existing_dockerfile(src, 80)
    with pytest.raises(AgentError, match="비밀"):
        buildfiles.normalize_dockerfile(df.dockerfile, df.container_port)


def test_reused_dockerfile_retains_project_ignore_and_security_rules(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Dockerfile").write_text(TWEET, encoding="utf-8")
    (src / ".dockerignore").write_text("wrong-precedence\n", encoding="utf-8")
    (src / "Dockerfile.dockerignore").write_text("excluded-build-input\n", encoding="utf-8")
    store = LocalStore(str(tmp_path / "output"))
    brain = FakeBrain(rec=recommendation(container_port=80),
                      df=DockerfileOut(dockerfile=TWEET, container_port=80))
    result = analyze.run({"project_id": "test", "analysis_id": "test", "source_uri": str(src)}, brain, store)
    ignore = Path(result["build_files"]["uri_prefix"], "dockerignore").read_text(encoding="utf-8")
    assert ignore.startswith("excluded-build-input\n")
    assert "wrong-precedence" not in ignore
    assert "**/.env" in ignore


def _bash():
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    return str(git_bash) if git_bash.exists() else shutil.which("bash")


@pytest.mark.parametrize("phase", ["pre_build", "post_build"])
@pytest.mark.parametrize("failure", ["none", "ecr", "gcp", "credentials"])
def test_parallel_registry_failures_propagate(phase, failure):
    bash = _bash()
    if not bash:
        pytest.skip("bash unavailable")
    cmds = yaml.safe_load(buildfiles.buildspec())["phases"][phase]["commands"]
    block = next(c for c in cmds if "ecr_pid=$!" in c)
    if phase == "pre_build":
        block = block.split("login_registries() {\n", 1)[1].split("\n}\n", 1)[0]
    # Fake shell functions exercise the generated commands, including pipefail.
    setup = r'''
set -o pipefail
ECR_REPO_URI=ecr/app; GCP_AR_REPO=gcp/app; IMAGE_TAG=test
aws() { if [ "$FAILURE" = credentials ]; then return 12; fi; printf password; }
docker() {
  case "$*" in
    *tag*) return 0 ;;
    *login*) cat >/dev/null ;;
  esac
  case "$*" in
    *ecr*) if [ "$FAILURE" = ecr ]; then return 13; fi ;;
    *gcp*) if [ "$FAILURE" = gcp ]; then return 14; fi ;;
  esac
  return 0
}
'''
    result = subprocess.run([bash, "-c", f"FAILURE={failure}\n" + setup + block], capture_output=True)
    should_fail = failure in ("ecr", "gcp") or (failure == "credentials" and phase == "pre_build")
    assert (result.returncode != 0) == should_fail, result.stderr.decode(errors="replace")


def test_all_buildspec_commands_parse_as_strings():
    for spec in (buildfiles.buildspec(), buildfiles.buildspec_images()):
        parsed = yaml.safe_load(spec)
        assert all(isinstance(c, str) for p in parsed["phases"].values() for c in p["commands"])
