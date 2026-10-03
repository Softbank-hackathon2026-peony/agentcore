"""analyze 속도 개선 (오프라인): 핵심 파일 미리 넣기, 이미지별 Dockerfile 동시 생성.

실제 모델 동작(추천+Dockerfile 한 번에, 캐싱)은 테스트 런타임에서 확인한다 (scripts/compare_runtimes.py)."""
import threading
from pathlib import Path

from agent import brain as brain_mod
from agent import source
from agent.handler import handle
from agent.scan import scan
from agent.storage import LocalStore
from tests.fakes import FakeBrain, recommendation

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
