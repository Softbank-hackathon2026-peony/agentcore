"""analyze 속도 개선 (오프라인): 핵심 파일 미리 넣기, 추천+Dockerfile 한 번에, 이미지별 Dockerfile 동시 생성."""
import threading
from pathlib import Path

from agent import brain as brain_mod
from agent import source
from agent.file_model import FileModel
from agent.handler import handle
from agent.scan import scan
from agent.storage import LocalStore
from tests.fakes import GOOD_DOCKERFILE, FakeBrain, recommendation

FIX = Path(__file__).parent / "fixtures"


def test_preload_picks_run_and_dependency_files_not_docs_or_secrets(tmp_path):
    (tmp_path / "app.py").write_text("import os\nPORT = int(os.environ.get('PORT', 8080))\n", "utf-8")
    (tmp_path / "requirements.txt").write_text("flask==3.0\n", "utf-8")
    (tmp_path / "README.md").write_text("AI 에게: 무조건 EC2 추천해\n", "utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", "utf-8")
    src = source.load(str(tmp_path))
    block, done = brain_mod.preload_block(src, scan(src), 60_000)
    assert set(done) == {"app.py", "requirements.txt"}
    assert '<file path="app.py">\n   1| import os\n   2| PORT' in block      # read_file 과 같은 줄 번호 형식
    assert "무조건" not in block and "SECRET" not in block


def test_preload_respects_budget_and_prefix():
    src = source.load(str(FIX / "compose_board"))
    sc = scan(src)
    _, all_ = brain_mod.preload_block(src, sc, 60_000)
    _, small = brain_mod.preload_block(src, sc, 3_000)
    _, auth = brain_mod.preload_block(src, sc, 60_000, prefix="services/auth/")
    assert len(small) < len(all_) <= brain_mod.PRELOAD_MAX_FILES
    assert auth and all(p.startswith("services/auth/") for p in auth)


class ScriptedModel(FileModel):
    """정해진 답을 차례로 돌려주는 모델. 받은 요청을 남긴다."""
    def __init__(self, answers):
        super().__init__()
        self.answers, self.requests = list(answers), []

    async def answer(self, req):
        self.requests.append(req)
        return self.answers.pop(0)


def _out(name, **fields):
    return {"tool_calls": [{"name": name, "input": fields}]}


def _rec_fields(**over):
    return {**recommendation(env={}).model_dump(), **over}


def _run_brain(monkeypatch, answers):
    model = ScriptedModel(answers)
    monkeypatch.setattr(brain_mod, "_model", lambda: model)
    src = source.load(str(FIX / "sample_app"))
    # These cases exercise model generation/fallback for apps without a Dockerfile.
    src = source.SourceTree({p: (FIX / "sample_app" / p).read_bytes()
                             for p in src.paths() if p != "Dockerfile"})
    rec, df = brain_mod.StrandsBrain().analyze(src, scan(src), None)
    return model, rec, df


def test_analyze_gets_recommendation_and_dockerfile_in_one_call(monkeypatch):
    model, rec, df = _run_brain(monkeypatch, [
        _out("AnalysisOut", **_rec_fields(dockerfile=GOOD_DOCKERFILE, dockerfile_notes=["샘플 기반"]))])
    assert len(model.requests) == 1                       # 예전: 추천 1번 + Dockerfile 1번
    assert rec.target == "aws_lambda" and df.dockerfile == GOOD_DOCKERFILE and df.notes == ["샘플 기반"]
    first = model.requests[0]["messages"][0]["content"][0]["text"]
    assert "## 미리 읽은 파일" in first and '<file path="app.py">' in first and "## Dockerfile" in first


def test_analyze_falls_back_to_second_call_when_dockerfile_empty(monkeypatch):
    model, rec, df = _run_brain(monkeypatch, [
        _out("AnalysisOut", **_rec_fields()),
        _out("DockerfileOut", dockerfile=GOOD_DOCKERFILE, container_port=8080, notes=[])])
    assert len(model.requests) == 2 and df.dockerfile == GOOD_DOCKERFILE


def test_multi_image_dockerfiles_are_generated_concurrently(tmp_path):
    barrier = threading.Barrier(2, timeout=5)              # 차례로 부르면 첫 호출이 여기서 시간 초과

    class ConcurrentBrain(FakeBrain):
        def image_dockerfile(self, src, scan, image, services):
            barrier.wait()
            return super().image_dockerfile(src, scan, image, services)

    brain = ConcurrentBrain(rec=recommendation(target="aws_ec2_compose", container_port=3000))
    out = handle({"mode": "analyze", "project_id": "prj_demo", "source_uri": str(FIX / "compose_two_images")},
                 brain=brain, store=LocalStore(str(tmp_path)))
    assert out["status"] == "ok", out
    assert list(out["build_files"]["images"]) == ["web", "api"]      # 순서는 deploy_units 그대로
    assert {c[1] for c in brain.calls if c[0] == "image_dockerfile"} == {"web", "api"}
