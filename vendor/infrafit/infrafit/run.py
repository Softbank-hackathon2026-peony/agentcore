"""실행 디렉터리, 단계 출력 쓰기, 입력 해시 캐시."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from infrafit.schema import SCHEMA_PATH, SchemaError, validate

STAGES: dict[str, tuple[str, str]] = {
    "S0": ("intake", "Intake"),
    "S1": ("inventory", "Inventory"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def input_hash(*parts) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def code_version() -> str:
    pkg = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(pkg.rglob("*.py")):
        digest.update(path.relative_to(pkg).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    digest.update(SCHEMA_PATH.read_bytes())
    return digest.hexdigest()[:12]


def dump_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@dataclass
class RunContext:
    run_id: str
    out_dir: Path
    cache_dir: Path

    @classmethod
    def create(cls, out_root: Path, run_id: str | None = None) -> "RunContext":
        rid = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = out_root / rid
        out_dir.mkdir(parents=True, exist_ok=True)
        cache_dir = out_root / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cls(run_id=rid, out_dir=out_dir, cache_dir=cache_dir)

    def _meta(self, stage: str, input_hash: str, started_at: str) -> dict:
        return {"run_id": self.run_id, "stage": stage, "input_hash": input_hash,
                "started_at": started_at, "finished_at": now_iso()}

    def write_stage(self, stage: str, body: dict, input_hash: str, started_at: str) -> dict:
        name, def_name = STAGES[stage]
        data = {"meta": self._meta(stage, input_hash, started_at), **body}
        validate(def_name, data)
        text = dump_json(data)
        (self.out_dir / f"{name}.json").write_text(text, encoding="utf-8")
        cache_file = self.cache_dir / stage / (input_hash.replace(":", "-") + ".json")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text, encoding="utf-8")
        return data

    def cached(self, stage: str, input_hash: str) -> dict | None:
        cache_file = self.cache_dir / stage / (input_hash.replace(":", "-") + ".json")
        if not cache_file.exists():
            return None
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        started = now_iso()
        data["meta"] = self._meta(stage, input_hash, started)
        name, def_name = STAGES[stage]
        try:
            validate(def_name, data)
        except SchemaError:
            return None
        (self.out_dir / f"{name}.json").write_text(dump_json(data), encoding="utf-8")
        return data
