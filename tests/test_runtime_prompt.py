"""Runtime guidance reaches every Dockerfile model request without AWS calls."""
import pytest

from agent import brain as brain_mod, source
from agent.scan import scan
from tests.fakes import GOOD_DOCKERFILE
from tests.test_speedup import FIX, ScriptedModel, _out, _rec_fields


@pytest.mark.parametrize("route", ["analyze", "fallback", "image", "fix"])
def test_nginx_runtime_guidance_reaches_model(monkeypatch, route):
    if route == "analyze":
        answers = [_out("AnalysisOut", **_rec_fields(dockerfile=GOOD_DOCKERFILE))]
    elif route == "fallback":
        answers = [_out("AnalysisOut", **_rec_fields()),
                   _out("DockerfileOut", dockerfile=GOOD_DOCKERFILE, container_port=8080)]
    elif route == "image":
        answers = [_out("DockerfileOut", dockerfile=GOOD_DOCKERFILE, container_port=8080)]
    else:
        answers = [_out("DockerfileFix", fixable=True, cause="invalid CMD",
                        dockerfile=GOOD_DOCKERFILE)]
    model = ScriptedModel(answers)
    monkeypatch.setattr(brain_mod, "_model", lambda: model)
    src = source.load(str(FIX / "sample_app"))
    if route in ("analyze", "fallback"):
        # 프로젝트 Dockerfile 이 고정 포트면 모델 답 대신 그대로 쓰므로(existing_dockerfile), 모델이 만드는 경로만 보려고 뺀다
        src = source.SourceTree({p: (FIX / "sample_app" / p).read_bytes() for p in src.paths() if p != "Dockerfile"})
    sc = scan(src)
    brain = brain_mod.StrandsBrain()

    if route in ("analyze", "fallback"):
        brain.analyze(src, sc, None)
    elif route == "image":
        brain.image_dockerfile(src, sc, {"id": "web", "context": "."}, [])
    else:
        brain.fix_dockerfile(src, sc, GOOD_DOCKERFILE, "invalid CMD", "BUILD")

    assert len(model.requests) == (2 if route == "fallback" else 1)
    for request in model.requests:
        prompt = request["messages"][0]["content"][0]["text"]
        assert "<<'EOF'" in prompt
        assert "${PORT}" in prompt and "$uri" in prompt
        assert "NGINX_ENVSUBST_FILTER=^PORT$" in prompt
        assert "/docker-entrypoint.sh" in prompt
        assert '["nginx", "-g", "daemon off;"]' in prompt
        assert "다른 환경변수도 필요한 기존 템플릿" in prompt
        assert "공식 entrypoint·템플릿 기능이 있다고 가정하지 마라" in prompt
