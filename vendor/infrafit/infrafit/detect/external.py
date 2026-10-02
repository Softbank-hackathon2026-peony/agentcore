"""외부 서비스(knowledge/signatures/external.yaml): 앱이 호출하는 LLM·인증·결제·메시징 등."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from infrafit import kb
from infrafit.detect.artifacts import ParsedArtifact, as_dict, is_build_path, pod_spec
from infrafit.detect.environments import compose_env
from infrafit.detect.manifests import Manifests
from infrafit.detect.signatures import _eval, is_aux_path
from infrafit.detect.testpaths import is_test_path
from infrafit.detect.workloads import WorkloadInfo
from infrafit.evidence import evidence
from infrafit.repo import Snapshot

MAX_ENV_EVIDENCE = 5
ENV_FILE_PATTERNS = (".env.example", ".env.sample", ".env.template", ".env.*.example")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
# 코드에서 환경변수 이름을 읽는 식
_CODE_ENV = (
    re.compile(r"process\.env\.([A-Za-z_]\w*)"),
    re.compile(r"process\.env\[\s*[\"']([A-Za-z_]\w*)[\"']\s*\]"),
    re.compile(r"import\.meta\.env\.([A-Za-z_]\w*)"),
    re.compile(r"os\.environ\[\s*[\"']([A-Za-z_]\w*)[\"']"),
    re.compile(r"os\.environ\.get\(\s*[\"']([A-Za-z_]\w*)[\"']"),
    re.compile(r"os\.getenv\(\s*[\"']([A-Za-z_]\w*)[\"']"),
    re.compile(r"System\.getenv\(\s*\"([A-Za-z_]\w*)\""),
)
_CODE_EXTS = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt")
_SPRING_PLACEHOLDER = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::[^}]*)?\}")
_SPRING_CONFIG = ("application*.yml", "application*.yaml", "application*.properties")
NON_USER_KINDS = ("reverse-proxy",)


def _is_env_file(name: str) -> bool:
    return any(fnmatchcase(name, p) for p in ENV_FILE_PATTERNS)


def _name_line(snap: Snapshot, rel: str, name: str) -> int | None:
    rx = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    return next((i for i, text in enumerate(snap.lines(rel), 1) if rx.search(text)), None)


def env_names(snap: Snapshot, artifacts: list[ParsedArtifact]) -> list[tuple[str, str, int | None]]:
    """저장소가 쓰는 환경변수 이름과 그 위치 (이름, 파일, 줄): 예시 .env 파일의 키, compose `environment` 키,
    k8s 컨테이너 `env[].name`, 코드의 환경변수 읽기, Spring 설정의 `${X}`. 테스트 경로는 보지 않는다."""
    out: list[tuple[str, str, int | None]] = []
    for rel in snap.files:
        if is_test_path(rel):
            continue
        name = PurePosixPath(rel).name
        if _is_env_file(name):
            for i, text in enumerate(snap.lines(rel), 1):
                if m := _ENV_LINE.match(text):
                    out.append((m.group(1), rel, i))
        elif name.endswith(_CODE_EXTS):
            for i, text in enumerate(snap.lines(rel), 1):
                if "env" not in text:
                    continue
                for rx in _CODE_ENV:
                    out += [(m.group(1), rel, i) for m in rx.finditer(text)]
        elif any(fnmatchcase(name, p) for p in _SPRING_CONFIG):
            for i, text in enumerate(snap.lines(rel), 1):
                out += [(m.group(1), rel, i) for m in _SPRING_PLACEHOLDER.finditer(text)]
    for art in artifacts:
        if is_test_path(art.path) or is_build_path(art.path) or not isinstance(art.objects, list):
            continue
        keys: list[str] = []
        if art.kind == "compose":
            for obj in art.objects:
                if isinstance(obj, tuple) and len(obj) == 2:
                    keys += list(compose_env(as_dict(obj[1]).get("environment")) or {})
        elif art.kind == "k8s":
            for doc in art.objects:
                containers = pod_spec(doc).get("containers") if isinstance(doc, dict) else None
                for c in containers if isinstance(containers, list) else []:
                    env = as_dict(c).get("env")
                    keys += [str(e["name"]) for e in (env if isinstance(env, list) else [])
                             if isinstance(e, dict) and isinstance(e.get("name"), str)]
        for key in dict.fromkeys(keys):
            out.append((key, art.path, _name_line(snap, art.path, key)))
    return sorted(set(out), key=lambda x: (is_aux_path(x[1]), x[1], x[2] or 0, x[0]))


def _under(path: str, root: str) -> bool:
    return root == "" or path == root or path.startswith(root + "/")


def find_external_services(snap: Snapshot, manifests: Manifests, artifacts: list[ParsedArtifact],
                           workloads: list[WorkloadInfo]) -> list[dict]:
    names = env_names(snap, artifacts)
    out: list[dict] = []
    for svc in kb.external():
        deps: list[dict] = []
        code: list[dict] = []
        envs: list[dict] = []
        secrets: set[str] = set()
        for leaf in kb.iter_conditions(svc["when"]):
            if "env" in leaf:
                rx = re.compile(leaf["env"])
                for name, rel, line in names:
                    if rx.search(name):
                        secrets.add(name)
                        if len(envs) < MAX_ENV_EVIDENCE:
                            envs.append(evidence(snap, rel, line, "tech"))
            elif "dependency" in leaf:
                deps += _eval(leaf, snap, manifests)
            else:
                code += _eval(leaf, snap, manifests)
        ev = [e for e in deps + code + envs if not is_test_path(e["path"])]
        ev = [e for i, e in enumerate(ev) if e not in ev[:i]]
        # 보조 코드 근거만으로는 만들지 않는다
        if not ev or all(is_aux_path(e["path"]) for e in ev):
            continue
        # 예시 env 파일의 자리표시자만 있으면 아직 쓰지 않는 서비스일 수 있다(코드·의존성·실제 설정 근거가 필요)
        if all(_is_env_file(PurePosixPath(e["path"]).name) for e in ev):
            continue
        ev.sort(key=lambda e: is_aux_path(e["path"]))
        confirmed = bool(code) or (bool(deps) and bool(envs))
        paths = {e["path"] for e in ev}
        used_by = sorted(w.id for w in workloads if w.kind not in NON_USER_KINDS and w.code_root is not None
                         and any(_under(p, w.code_root) for p in paths))
        out.append({"id": svc["id"], "label": svc["label"], "kind": svc["kind"], "used_by": used_by,
                    "secrets": sorted(secrets), "evidence": ev,
                    "status": "confirmed" if confirmed else "candidate"})
    return sorted(out, key=lambda s: s["id"])
