"""로컬 전용 모델: Bedrock 대신 요청·응답을 파일로 주고받는다 (PAWPLOY_LLM=file).

에이전트 루프(도구 호출·구조화 출력·턴 수)는 Strands 가 실제와 똑같이 돌리고, 모델 답만 사람(또는 다른 도구)이 쓴다.
AWS 자격 증명 없이 analyze 흐름을 끝까지 돌려 보거나, 모델 왕복 수를 세는 데 쓴다.

  요청: $PAWPLOY_LLM_DIR/req-<번호>.json   {id, system, tool_choice, tools[{name, description, input_schema}], messages}
  응답: $PAWPLOY_LLM_DIR/res-<번호>.json   {"text": "...", "tool_calls": [{"name": "read_file", "input": {...}}]}
        (tool_calls 가 있으면 stopReason=tool_use, 없으면 end_turn)
"""
import asyncio
import itertools
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from strands.models import Model

LLM_DIR = Path(os.environ.get("PAWPLOY_LLM_DIR", ".local-llm"))
TIMEOUT_S = int(os.environ.get("PAWPLOY_LLM_TIMEOUT", "1800"))
_seq = itertools.count(1)
_lock = threading.Lock()


class FileModel(Model):
    def __init__(self):
        self.config: dict[str, Any] = {"model_id": "file"}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> Any:
        return self.config

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("FileModel 은 structured_output_model(도구 방식)만 지원")

    async def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
        req = {"system": system_prompt, "tool_choice": tool_choice,
               "tools": [{"name": t["name"], "description": t.get("description"),
                          "input_schema": t.get("inputSchema", {}).get("json")} for t in tool_specs or []],
               "messages": messages}
        t0 = time.time()
        res = await self.answer(req)
        yield {"messageStart": {"role": "assistant"}}
        if res.get("text"):
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": res["text"]}}}
            yield {"contentBlockStop": {}}
        calls = res.get("tool_calls") or []
        for c in calls:
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"tooluse_{uuid.uuid4().hex[:12]}",
                                                               "name": c["name"]}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(c.get("input") or {},
                                                                                  ensure_ascii=False)}}}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use" if calls else "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                            "metrics": {"latencyMs": int((time.time() - t0) * 1000)}}}

    async def answer(self, req: dict) -> dict:
        with _lock:
            rid = f"{next(_seq):04d}"
        LLM_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LLM_DIR / f".req-{rid}.json"
        tmp.write_text(json.dumps({"id": rid, **req}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.rename(LLM_DIR / f"req-{rid}.json")          # 다 쓴 뒤에 보이게
        res_path, t0 = LLM_DIR / f"res-{rid}.json", time.time()
        while True:
            if res_path.exists():
                try:
                    return json.loads(res_path.read_text(encoding="utf-8"))
                except ValueError:                        # 쓰는 중
                    pass
            if time.time() - t0 > TIMEOUT_S:
                raise TimeoutError(f"{res_path} 응답이 {TIMEOUT_S}초 안에 오지 않음")
            await asyncio.sleep(0.5)
