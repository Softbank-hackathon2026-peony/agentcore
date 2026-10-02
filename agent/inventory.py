"""InfraFit S1 인벤토리 (LLM 없음, 규칙 기반).

vendor/infrafit 의 InfraFit을 별도 프로세스로 S0+S1까지 돌려서 inventory.json 을 읽고,
LLM 프롬프트에 넣을 수 있게 작게(약 8KB 이하) 요약한다.

- 소스는 임시 폴더에 풀어서 넘긴다. 비밀 파일(.env, 키 등)은 쓰지 않는다.
  .env.example 류는 변수 이름만 남기고 값은 지운 채로 쓴다 (줄 번호는 그대로).
- 별도 프로세스 + 시간 제한: InfraFit이 멈추거나 죽어도 analyze 는 계속된다.
- 절대 예외를 던지지 않는다. 실패하면 {"status": "error"|"timeout", "message": ...}.
- 요약의 file:line 은 inventory.json 의 근거를 그대로 옮긴 것이다 (실제 저장소 경로).
"""
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .source import SourceTree, is_secret

CODE_ROOT = Path(__file__).resolve().parent.parent        # 배포 zip 루트 (의존성도 여기 설치됨)
VENDOR_DIR = CODE_ROOT / "vendor" / "infrafit"
MAX_SUMMARY_BYTES = 8000
ENV_EXAMPLE_FILES = {".env.example", ".env.sample", ".env.template", "env.example"}
EXPLICIT_SETTING = re.compile(r"timeout|body", re.I)
RUN_ID = "run"

_RUNNER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from infrafit.pipeline import analyze\n"
    "analyze(sys.argv[1], Path(sys.argv[2]), until='S1', run_id=sys.argv[3])\n"
)


def run_inventory(src: SourceTree, timeout_s: int = 60) -> dict:
    try:
        with tempfile.TemporaryDirectory(prefix="pawploy-inv-") as tmp:
            repo, out = Path(tmp) / "repo", Path(tmp) / "out"
            _materialize(src, repo)
            env = {**os.environ, "PYTHONPATH": os.pathsep.join(
                [str(VENDOR_DIR), str(CODE_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])])}
            try:
                proc = subprocess.run([sys.executable, "-c", _RUNNER, str(repo), str(out), RUN_ID],
                                      capture_output=True, text=True, timeout=timeout_s, env=env, cwd=tmp)
            except subprocess.TimeoutExpired:
                return {"status": "timeout", "message": f"InfraFit 인벤토리가 {timeout_s}초 안에 끝나지 않음"}
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["(출력 없음)"]
                return {"status": "error", "message": f"InfraFit 실패 (exit {proc.returncode}): {tail[0][:300]}"}
            inventory = json.loads((out / RUN_ID / "inventory.json").read_text(encoding="utf-8"))
            return {"status": "ok", "infrafit_commit": _commit(), "summary": summarize(inventory)}
    except Exception as e:  # noqa: BLE001 — 인벤토리는 보조 정보라 어떤 실패도 analyze 를 막지 않는다
        return {"status": "error", "message": f"{type(e).__name__}: {str(e)[:300]}"}


def _materialize(src: SourceTree, root: Path) -> None:
    root.mkdir(parents=True)
    base = root.resolve()
    for p in src.paths():
        name = posixpath.basename(p).lower()
        if name in ENV_EXAMPLE_FILES:
            data = _names_only(src._files[p])  # noqa: SLF001 — 같은 패키지 내부 접근 (scan._raw_lines 와 같음)
        elif is_secret(p):
            continue
        else:
            data = src._files[p]  # noqa: SLF001
        dest = (root / p).resolve()
        if base not in dest.parents:       # SourceTree 가 이미 막지만 한 번 더
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def _names_only(data: bytes) -> bytes:
    """KEY=value → KEY= (값 제거, 줄 수 유지)."""
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        out.append(f"{m.group(1)}=" if m else "")
    return ("\n".join(out) + "\n").encode("utf-8")


def _commit() -> str:
    try:
        for line in (VENDOR_DIR / "SOURCE").read_text(encoding="utf-8").splitlines():
            if line.startswith("commit:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


# ---------------- 요약 ----------------

def _at(ev: dict | None) -> str | None:
    if not ev or not ev.get("path"):
        return None
    return f"{ev['path']}:{ev['line']}" if ev.get("line") else ev["path"]


def _ats(evs, n: int = 2) -> list[str]:
    return [a for a in (_at(e) for e in (evs or [])[:n]) if a]


def summarize(inv: dict) -> dict:
    eps = inv.get("endpoints") or []
    per_workload: dict[str, int] = {}
    for e in eps:
        per_workload[e.get("workload", "?")] = per_workload.get(e.get("workload", "?"), 0) + 1

    def endpoint_line(e: dict) -> str:
        s = f"{e.get('method', '?')} {str(e.get('route', ''))[:100]} @{_at(e.get('handler')) or '?'}"
        exp = [f"{x.get('environment') or '-'}:{x.get('value')}" for x in e.get("exposure") or []]
        return s + (f" [{', '.join(exp[:3])}]" if exp else "")

    paths = {}
    for rp in inv.get("request_paths") or []:
        hops = sorted(rp.get("hops") or [], key=lambda h: h.get("order", 0))
        settings = []
        for h in hops:
            for st in h.get("settings") or []:
                if not st.get("defaulted") and EXPLICIT_SETTING.search(st.get("key", "")):
                    where = _at(st.get("evidence"))
                    settings.append(f"{st['key']}={str(st.get('value'))[:40]}" + (f" @{where}" if where else ""))
        paths[rp["id"]] = {"workload": rp.get("workload"), "environment": rp.get("environment"),
                           "hops": [f"{h.get('kind')} {h.get('component')}" for h in hops][:8],
                           "settings": settings[:8]}

    summary = {
        "workloads": [{"id": w["id"], "kind": w.get("kind"), "name": w.get("name"), "status": w.get("status"),
                       "at": _at(w.get("entrypoint"))} for w in inv.get("workloads") or []][:20],
        "endpoints": {"total": len(eps), "per_workload": dict(list(per_workload.items())[:20]),
                      "first": [endpoint_line(e) for e in eps[:25]]},
        "datastores": [{"id": d["id"], "role": d.get("role"), "component": _component_of(inv, d["id"]),
                        "used_by": d.get("used_by", []), "status": d.get("status"), "at": _ats(d.get("evidence"))}
                       for d in inv.get("datastores") or []][:15],
        "external_services": [{"id": x["id"], "kind": x.get("kind"), "secrets": x.get("secrets", [])[:8],
                               "used_by": x.get("used_by", []), "status": x.get("status"),
                               "at": _ats(x.get("evidence"), 1)}
                              for x in inv.get("external_services") or []][:15],
        "environments": [{"name": e.get("name"), "kind": e.get("kind"), "members": e.get("members", []),
                          "manual": e.get("manual", False), "at": _at(e.get("source"))}
                         for e in inv.get("environments") or []][:10],
        "compute": [{"scope": c.get("scope"), "component": c.get("component"), "environment": c.get("environment")}
                    for c in inv.get("current_components") or []
                    if str(c.get("component", "")).startswith("cp:")][:15],
        "request_paths": dict(list(paths.items())[:10]),
        "unmapped": [u.get("label") for u in inv.get("unmapped") or []][:20],
    }
    return _bound(summary)


def _component_of(inv: dict, scope: str) -> str | None:
    return next((c.get("component") for c in inv.get("current_components") or [] if c.get("scope") == scope), None)


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _bound(summary: dict) -> dict:
    """크기 상한을 넘으면 가장 긴 목록부터 반씩 줄인다."""
    def lists(s):
        yield "endpoints.first", s["endpoints"], "first"
        for k in ("workloads", "datastores", "external_services", "environments", "compute", "unmapped"):
            yield k, s, k
        yield "request_paths", s, "request_paths"

    while _size(summary) > MAX_SUMMARY_BYTES:
        name, holder, key = max(lists(summary), key=lambda t: _size(t[1][t[2]]))
        items = holder[key]
        if not items:                      # 더 줄일 목록이 없다
            break
        if len(items) <= 1:
            holder[key] = type(items)()
        elif isinstance(items, dict):
            holder[key] = dict(list(items.items())[: len(items) // 2])
        else:
            holder[key] = items[: len(items) // 2]
        summary.setdefault("truncated", [])
        if name not in summary["truncated"]:
            summary["truncated"].append(name)
    return summary
