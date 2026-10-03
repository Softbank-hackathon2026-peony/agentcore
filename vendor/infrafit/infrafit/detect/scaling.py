"""워크로드별 확장 요구(계획 2b Task B): 레플리카·HPA·KEDA·compose deploy.replicas와 부하 테스트 스크립트.

결과(워크로드 `scaling`): {min, max, autoscale, evidence[], load_tests[]}.
- 환경(렌더된 kustomize 결과 하나, 또는 렌더하지 않은 k8s 파일의 디렉터리 하나, compose 파일 하나)마다
  min·max를 정한다: 대상에 HPA·KEDA가 있으면 그 범위(autoscale), 없으면 Deployment·StatefulSet `replicas`
  (compose는 `deploy.replicas`·`scale`). 환경들 중 최댓값을 쓴다(min도 max도 각각 최댓값, autoscale은 하나라도).
- HPA minReplicas 기본값 1, KEDA minReplicaCount 기본값 0·maxReplicaCount 기본값 100(각 API 기본값).
- 레플리카 설정이 없으면 min=max=1, evidence=[](설정 없음). 부하 테스트는 레플리카 근거가 아니라 load_tests[]에
  따로 둔다(웹·실시간 워크로드에 붙인다: 스크립트가 어느 워크로드를 치는지는 코드로 정하지 않는다).
- 레플리카 근거도 부하 테스트도 없는 워크로드에는 scaling을 내지 않는다.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

import yaml

from infrafit.detect.artifacts import ParsedArtifact, as_dict, build_source, is_build_path
from infrafit.detect.compose import DEVCONTAINER, service_line
from infrafit.detect.testpaths import is_test_path
from infrafit.detect.workloads import WorkloadInfo
from infrafit.evidence import evidence
from infrafit.repo import Snapshot, parent_dir

MAX_EVIDENCE = 10
HPA_MIN_DEFAULT = 1
KEDA_MIN_DEFAULT = 0
KEDA_MAX_DEFAULT = 100
SCALE_TARGET_KINDS = ("Deployment", "StatefulSet")
LOAD_TEST_KINDS = ("web", "realtime")

K6_IMPORT = re.compile(r"""^\s*import\s+.*\bfrom\s+['"]k6(?:/[\w-]+)?['"]|require\(\s*['"]k6(?:/[\w-]+)?['"]\s*\)""",
                       re.MULTILINE)
LOCUST_IMPORT = re.compile(r"^\s*(?:from\s+locust\s+import\b|import\s+locust\b)", re.MULTILINE)
JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts")


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _key_line(snap: Snapshot, rel: str, name: str, key: str) -> int | None:
    """`name: <name>` 줄 다음에 처음 나오는 `<key>:` 줄(같은 파일에 문서가 여럿이어도 그 객체의 줄)."""
    after = None
    name_rx = re.compile(rf"^\s*name:\s*[\"']?{re.escape(name)}[\"']?\s*(?:#.*)?$")
    key_rx = re.compile(rf"^\s*{re.escape(key)}\s*:")
    for i, text in enumerate(snap.lines(rel), 1):
        if after is None:
            if name_rx.match(text) or re.search(rf"\{{\s*name:\s*{re.escape(name)}\s*[,}}]", text):
                after = i
                if key_rx.match(text):
                    return i
        elif text.startswith("---"):
            after = None
        elif key_rx.match(text):
            return i
    return None


def _ev(snap: Snapshot, art: ParsedArtifact, obj_kind: str, obj_name: str, keys: dict) -> dict:
    """설정 근거: 렌더 결과는 kustomization.yaml(줄 없음, 렌더된 값을 snippet에), 파일은 첫 키 줄."""
    if is_build_path(art.path):
        vals = ", ".join(f"{k}: {v}" for k, v in keys.items())
        return {"path": build_source(art.path), "line": None,
                "snippet": f"kustomize build: {obj_kind}/{obj_name} {vals}"[:200]}
    line = next((ln for k in keys if (ln := _key_line(snap, art.path, obj_name, k))), None)
    return evidence(snap, art.path, line)


def _k8s_groups(artifacts: list[ParsedArtifact]) -> list[list[ParsedArtifact]]:
    """환경 단위: 렌더 결과는 하나씩, 렌더하지 않은 파일은 디렉터리마다 묶는다(HPA와 Deployment가 다른 파일인 경우)."""
    groups: dict[str, list[ParsedArtifact]] = {}
    for art in artifacts:
        if art.kind != "k8s" or not art.parsed or is_test_path(build_source(art.path)):
            continue
        key = art.path if is_build_path(art.path) else "dir:" + parent_dir(art.path)
        groups.setdefault(key, []).append(art)
    return [groups[k] for k in sorted(groups)]


def _k8s_env(snap: Snapshot, group: list[ParsedArtifact]) -> dict[str, dict]:
    """대상 이름 → {min, max, autoscale, evidence}(이 환경 하나)."""
    replicas: dict[str, tuple[int, dict]] = {}
    auto: dict[str, tuple[int, int, list[dict]]] = {}
    for art in group:
        for doc in art.objects:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            name = as_dict(doc.get("metadata")).get("name")
            spec = as_dict(doc.get("spec"))
            if not isinstance(name, str) or not name:
                continue
            if kind in SCALE_TARGET_KINDS and _int(spec.get("replicas")) is not None:
                r = spec["replicas"]
                replicas[name] = (r, _ev(snap, art, kind, name, {"replicas": r}))
                continue
            ref = as_dict(spec.get("scaleTargetRef"))
            target = ref.get("name")
            if not isinstance(target, str) or not target or ref.get("kind", "Deployment") not in SCALE_TARGET_KINDS:
                continue
            if kind == "HorizontalPodAutoscaler" and str(doc.get("apiVersion", "")).startswith("autoscaling/"):
                lo = _int(spec.get("minReplicas"))
                hi = _int(spec.get("maxReplicas"))
                if hi is None:
                    continue
                lo = HPA_MIN_DEFAULT if lo is None else lo
                keys = {k: spec[k] for k in ("minReplicas", "maxReplicas") if k in spec}
            elif kind == "ScaledObject" and str(doc.get("apiVersion", "")).startswith("keda.sh/"):
                lo = _int(spec.get("minReplicaCount"))
                hi = _int(spec.get("maxReplicaCount"))
                lo = KEDA_MIN_DEFAULT if lo is None else lo
                hi = KEDA_MAX_DEFAULT if hi is None else hi
                keys = {k: spec[k] for k in ("minReplicaCount", "maxReplicaCount") if k in spec} or {"scaleTargetRef": target}
            else:
                continue
            ev = _ev(snap, art, kind, name, keys)
            prev = auto.get(target)
            # 같은 대상에 오토스케일러가 둘이면(HPA + KEDA가 만든 것 등) 넓은 쪽
            auto[target] = (max(lo, prev[0]), max(hi, prev[1]), prev[2] + [ev]) if prev else (lo, hi, [ev])
    out: dict[str, dict] = {}
    for target in sorted(set(replicas) | set(auto)):
        if target in auto:
            lo, hi, evs = auto[target]
            out[target] = {"min": lo, "max": max(lo, hi), "autoscale": hi > lo, "evidence": evs}
        else:
            r, ev = replicas[target]
            out[target] = {"min": r, "max": r, "autoscale": False, "evidence": [ev]}
    return out


def _compose_envs(snap: Snapshot, artifacts: list[ParsedArtifact]) -> list[dict[str, dict]]:
    """compose 파일마다 서비스 이름 → 레플리카(deploy.replicas, 없으면 scale)."""
    envs: list[dict[str, dict]] = []
    for art in sorted((a for a in artifacts if a.kind == "compose" and a.parsed), key=lambda a: a.path):
        if DEVCONTAINER in PurePosixPath(art.path).parts or is_test_path(art.path):
            continue
        env: dict[str, dict] = {}
        for obj in art.objects:
            if not isinstance(obj, tuple) or len(obj) != 2:
                continue
            name, svc = obj[0], as_dict(obj[1])
            r = _int(as_dict(svc.get("deploy")).get("replicas"))
            key = "replicas"
            if r is None:
                r, key = _int(svc.get("scale")), "scale"
            if r is None:
                continue
            start = service_line(snap, art.path, str(name))
            line = None
            if start is not None:
                rx = re.compile(rf"^\s*{key}\s*:")
                line = next((i for i, t in enumerate(snap.lines(art.path), 1) if i > start and rx.match(t)), None)
            env[str(name)] = {"min": r, "max": r, "autoscale": False,
                              "evidence": [evidence(snap, art.path, line or start)]}
        envs.append(env)
    return envs


def _artillery(snap: Snapshot, rel: str) -> int | None:
    try:
        data = yaml.safe_load(snap.read(rel))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    cfg = as_dict(data.get("config"))
    if "target" in cfg and ("phases" in cfg or "scenarios" in data):
        return next((i for i, t in enumerate(snap.lines(rel), 1) if t.startswith("config:")), 1)
    return None


def find_load_tests(snap: Snapshot) -> list[dict]:
    """부하 테스트 스크립트(k6·locust·artillery)의 근거 줄. 테스트 디렉터리에 있어도 센다(보통 거기 둔다)."""
    out: list[dict] = []
    for rel in snap.files:
        name = PurePosixPath(rel).name
        line = None
        if name.endswith(JS_SUFFIXES) and "node_modules" not in rel:
            text = snap.read(rel)
            if "k6" in text and (m := K6_IMPORT.search(text)):
                line = text.count("\n", 0, m.start()) + 1
        elif name.endswith(".py"):
            text = snap.read(rel)
            if "locust" in text and (m := LOCUST_IMPORT.search(text)):
                line = text.count("\n", 0, m.start()) + 1
        elif name.endswith((".yml", ".yaml")):
            text = snap.read(rel)
            if "target:" in text and ("phases:" in text or "scenarios:" in text):
                line = _artillery(snap, rel)
        if line is not None:
            out.append(evidence(snap, rel, line, "tech"))
    return out


def _merge(acc: dict | None, cur: dict) -> dict:
    if acc is None:
        return {"min": cur["min"], "max": cur["max"], "autoscale": cur["autoscale"], "evidence": list(cur["evidence"])}
    acc["min"] = max(acc["min"], cur["min"])
    acc["max"] = max(acc["max"], cur["max"])
    acc["autoscale"] = acc["autoscale"] or cur["autoscale"]
    acc["evidence"] += [e for e in cur["evidence"] if e not in acc["evidence"]]
    return acc


def attach_scaling(snap: Snapshot, artifacts: list[ParsedArtifact], workloads: list[WorkloadInfo]) -> None:
    """워크로드마다 scaling을 정한다(WorkloadInfo.scaling, 근거가 없으면 None)."""
    k8s_envs = [_k8s_env(snap, g) for g in _k8s_groups(artifacts)]
    compose_envs = _compose_envs(snap, artifacts)
    load_tests = find_load_tests(snap)
    for w in workloads:
        envs = k8s_envs if w.source == "k8s" else compose_envs if w.source == "compose" else []
        acc = None
        for env in envs:
            if w.name in env:
                acc = _merge(acc, env[w.name])
        if acc is not None:
            acc["max"] = max(acc["max"], acc["min"])
            acc["evidence"] = acc["evidence"][:MAX_EVIDENCE]
        lt = load_tests[:MAX_EVIDENCE] if w.kind in LOAD_TEST_KINDS else []
        if acc is None and not lt:
            continue
        acc = acc or {"min": 1, "max": 1, "autoscale": False, "evidence": []}
        if lt:
            acc["load_tests"] = lt
        w.scaling = acc
