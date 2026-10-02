"""mode 분기. AgentCore 진입점(app.py)과 테스트가 같은 함수를 부른다."""
import traceback

from . import analyze, fix_build, terraform
from .errors import AgentError
from .storage import Store, default_store

MODES = {
    "analyze": analyze.run,           # 05~08
    "fix_build": fix_build.run,       # 11~13
    "gen_terraform": terraform.gen,   # 18~20
    "fix_terraform": terraform.fix,   # 23~25
}


def handle(payload: dict, brain=None, store: Store | None = None) -> dict:
    if not isinstance(payload, dict):
        return _error("bad_request", "payload는 JSON 객체여야 합니다")
    mode = payload.get("mode")
    fn = MODES.get(mode)
    if fn is None:
        return _error("unknown_mode", f"지원하지 않는 mode: {mode!r} (가능: {sorted(MODES)})")
    if brain is None:
        from .brain import StrandsBrain
        brain = StrandsBrain()
    try:
        return fn(payload, brain, store or default_store())
    except AgentError as e:
        return _error(e.code, e.message, mode)
    except Exception as e:  # 예상 못 한 오류도 같은 형식으로
        traceback.print_exc()
        return _error("internal", f"{type(e).__name__}: {str(e)[:300]}", mode)


def _error(code: str, message: str, mode=None) -> dict:
    return {"status": "error", "mode": mode, "error": {"code": code, "message": message}}
