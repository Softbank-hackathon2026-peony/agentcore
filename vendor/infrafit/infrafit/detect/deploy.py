"""환경별 배포 대상: 플랫폼 설정 파일 하나(`platform`), GitHub Actions 배포 step 하나(`ci-deploy`)가 각각
환경 하나다. 환경마다 배포하는 워크로드(members)와 그 compute를 기록한다."""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

import yaml

from infrafit import kb
from infrafit.detect.artifacts import ParsedArtifact, as_dict
from infrafit.detect.components import NON_BACKEND_KINDS, PLATFORM_COMPUTE, STATIC_HOSTING_FILES
from infrafit.detect.environments import Environment, workload_in
from infrafit.detect.images import image_name
from infrafit.detect.testpaths import is_test_path
from infrafit.detect.workloads import WorkloadInfo, workload_dockerfile
from infrafit.evidence import evidence, line_of
from infrafit.repo import Snapshot, parent_dir

PLATFORM_NAMES = {"vercel.json": "vercel", "netlify.toml": "netlify", "fly.toml": "fly", "render.yaml": "render",
                  "railway.json": "railway", "railway.toml": "railway", "railway.ts": "railway", "app.yaml": "gae"}
GAE_LABEL = "Google App Engine (app.yaml)"
# 이 트리거 중 하나라도 있으면 자동 실행이다(없으면 수동 실행만)
AUTO_TRIGGERS = {"push", "release", "schedule", "workflow_run"}
_COMPOSE_FILE = re.compile(r"(?<![\w.-])((?:[\w.-]+/)*(?:docker-)?compose(?:[.-][\w-]+)*\.ya?ml)\b")
_COMPOSE_CMD = re.compile(r"\bdocker(?:-|\s+)compose\b")
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=(\S+)\s*$")
_GITHUB_ENV = re.compile(r"echo\s+[\"']?([A-Za-z_]\w*)=([^\"'>]+)[\"']?\s*>>\s*[\"']?\$\{?GITHUB_ENV")
# docker build·helm upgrade에서 값을 받는 옵션(그 다음 토큰은 위치 인자가 아니다)
_VALUE_OPTS = {"-t", "--tag", "-f", "--file", "--build-arg", "--target", "--platform", "--label", "--secret",
               "--cache-from", "--cache-to", "--network", "--progress", "--output", "-o", "--values", "-n",
               "--namespace", "--set", "--set-string", "--set-file", "--version", "--timeout", "--kube-context",
               "--repo", "--region", "--project", "--image", "--source", "--service", "--config", "--cwd",
               "--port", "--set-env-vars", "--update-secrets", "--env-vars-file", "-k",
               "--kustomize", "--filename"}


@dataclass
class DeployTarget:
    env: Environment
    components: list[dict] = field(default_factory=list)  # 멤버마다 compute 기록(scope·environment 포함)
    unmapped: dict | None = None


def _norm(path: str, base: str = "") -> str | None:
    """저장소 경로로 정규화. 변수가 남았거나 저장소 밖이면 None."""
    path = path.strip().strip("\"'")
    if not path or "$" in path or path.startswith(("/", "~")):
        return None
    out = posixpath.normpath(posixpath.join(base, path))
    if out.startswith(".."):
        return None
    return "" if out == "." else out


def _under(path: str, root: str) -> bool:
    return root == "" or path == root or path.startswith(root + "/")


def _compute_entry(w: WorkloadInfo, component: str, label: str | None, platform_file: str | None,
                   env: str, status: str, ev: list[dict]) -> dict:
    """멤버 하나의 compute 기록. 정적 프런트엔드를 정적 호스팅 플랫폼이 배포하면 함수 compute가 아니다."""
    if w.kind == "static-frontend" and platform_file in STATIC_HOSTING_FILES:
        component, label = "unmapped", f"static hosting ({platform_file})"
    entry = {"scope": w.id, "component": component, "environment": env, "settings": [], "evidence": ev,
             "status": status}
    if component == "unmapped":
        entry["label"] = label or "unknown compute"
    return entry


def _members(workloads: list[WorkloadInfo]) -> list[WorkloadInfo]:
    return [w for w in workloads if w.kind != "reverse-proxy"]


# --- platform ------------------------------------------------------------------

def _gae_configs(snap: Snapshot) -> list[str]:
    """App Engine app.yaml: 최상위에 runtime이 있고 쿠버네티스 객체(apiVersion·kind)가 아닌 것."""
    out = []
    for rel in snap.files:
        if PurePosixPath(rel).name != "app.yaml" or is_test_path(rel):
            continue
        try:
            data = as_dict(yaml.safe_load(snap.read(rel)))
        except yaml.YAMLError:
            continue
        if "runtime" in data and "apiVersion" not in data and "kind" not in data:
            out.append(rel)
    return out


def _config_dir(rel: str) -> str:
    return "" if rel == ".railway/railway.ts" else parent_dir(rel)


def _render_members(snap: Snapshot, art: ParsedArtifact, d: str, candidates: list[WorkloadInfo],
                    artifacts: list[ParsedArtifact]) -> dict[str, tuple[str, dict]]:
    """render.yaml 서비스의 rootDir·dockerfilePath·dockerContext로 워크로드를 찾는다. 셋 다 없으면 설정 디렉터리가
    code_root인 워크로드(추측). → 워크로드 id → (상태, 근거)."""
    data = as_dict(art.objects[0][1]) if art.parsed and art.objects and isinstance(art.objects[0], tuple) else {}
    services = data.get("services")
    out: dict[str, tuple[str, dict]] = {}
    for svc in services if isinstance(services, list) else []:
        svc = as_dict(svc)
        name = svc.get("name")
        line = line_of(snap, art.path, f"name: {name}") if isinstance(name, str) else None
        ev = evidence(snap, art.path, line)
        root = _norm(str(svc["rootDir"]), d) if isinstance(svc.get("rootDir"), str) else None
        hints = [svc.get(k) for k in ("rootDir", "dockerfilePath", "dockerContext")]
        dfs = set()
        if isinstance(svc.get("dockerfilePath"), str):
            dfs = {p for p in (_norm(svc["dockerfilePath"], root or d), _norm(svc["dockerfilePath"], d)) if p is not None}
        ctx = _norm(str(svc["dockerContext"]), root or d) if isinstance(svc.get("dockerContext"), str) else None
        for w in candidates:
            df = workload_dockerfile(w, artifacts)
            if (df is not None and df.path in dfs) or (root is not None and w.code_root == root) \
                    or (ctx is not None and dfs == set() and w.code_root == ctx):
                out.setdefault(w.id, ("confirmed", ev))
            elif not any(isinstance(h, str) for h in hints) and w.code_root == d:
                out.setdefault(w.id, ("candidate", ev))
    return out


def platform_targets(snap: Snapshot, artifacts: list[ParsedArtifact], workloads: list[WorkloadInfo]) -> list[DeployTarget]:
    configs = [(a.path, a) for a in artifacts if a.kind == "platform-config"
               and PurePosixPath(a.path).name in PLATFORM_COMPUTE and not is_test_path(a.path)]
    configs += [(rel, None) for rel in _gae_configs(snap)]
    candidates = [w for w in _members(workloads) if w.code_root is not None]
    out: list[DeployTarget] = []
    for rel, art in sorted(configs, key=lambda c: c[0]):
        fname = PurePosixPath(rel).name
        platform = PLATFORM_NAMES[fname]
        d = _config_dir(rel)
        name = f"{platform}/{d}" if d else platform
        if fname == "render.yaml" and art is not None:
            members = _render_members(snap, art, d, candidates, artifacts)
        else:
            # 같은 플랫폼의 더 깊은 설정이 품는 워크로드는 그 설정의 것이다
            same = [_config_dir(r) for r, _ in configs if PLATFORM_NAMES[PurePosixPath(r).name] == platform]
            inside = [w for w in candidates if _under(w.code_root, d)
                      and max((x for x in same if _under(w.code_root, x)), key=len) == d]
            ev = evidence(snap, rel)
            members = {w.id: ("confirmed" if w.code_root == d or len(inside) == 1 else "candidate", ev)
                       for w in inside}
        if not members:
            continue
        env = Environment(name, rel, True, kind="platform", members={wid: "" for wid in members})
        by_id = {w.id: w for w in workloads}
        component = PLATFORM_COMPUTE.get(fname, "unmapped")
        comps = [_compute_entry(by_id[wid], component, GAE_LABEL if platform == "gae" else None, fname, name,
                                status, [ev]) for wid, (status, ev) in sorted(members.items())]
        out.append(DeployTarget(env, comps))
    return out


# --- ci-deploy -----------------------------------------------------------------

def _triggers(data: dict) -> set[str]:
    on = data.get("on", data.get(True))  # YAML 1.1은 `on`을 True로 읽는다
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(x) for x in on}
    return {str(k) for k in on} if isinstance(on, dict) else set()


def _step_nodes(snap: Snapshot, rel: str) -> dict[tuple[str, int], yaml.MappingNode]:
    """(job, step 순번) → step 매핑 노드(줄 위치를 얻는다)."""
    try:
        root = yaml.compose(snap.read(rel))
    except yaml.YAMLError:
        return {}

    def get(node, key):
        if not isinstance(node, yaml.MappingNode):
            return None
        return next((v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == key), None)

    out = {}
    jobs = get(root, "jobs")
    for k, job in jobs.value if isinstance(jobs, yaml.MappingNode) else []:
        steps = get(job, "steps")
        for i, step in enumerate(steps.value if isinstance(steps, yaml.SequenceNode) else []):
            if isinstance(step, yaml.MappingNode):
                out[(str(k.value), i)] = step
    return out


def _step_line(snap: Snapshot, rel: str, node: yaml.MappingNode | None, key: str, rx: re.Pattern | None) -> int | None:
    """step의 `run`·`uses` 줄. run이면 그 안에서 정규식이 처음 맞는 줄."""
    if node is None:
        return None
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            if rx is not None:
                lines = snap.lines(rel)
                for i in range(k.start_mark.line, min(v.end_mark.line + 1, len(lines))):
                    if rx.search(lines[i]):
                        return i + 1
            return k.start_mark.line + 1
    return None


def _subst(text: str, env: dict[str, str]) -> str:
    for _ in range(3):
        new = re.sub(r"\$\{\{\s*env\.(\w+)\s*\}\}", lambda m: env.get(m.group(1), m.group(0)), text)
        new = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), new)
        if new == text:
            break
        text = new
    return text


def _env_map(value) -> dict[str, str]:
    return {str(k): str(v) for k, v in as_dict(value).items() if isinstance(v, (str, int, float))}


def _commands(run: str) -> list[list[str]]:
    """run 스크립트의 명령들(줄 잇기 처리, `&&`·`;`·`|`로 나눈 토큰 목록)."""
    out = []
    for line in run.replace("\\\n", " ").splitlines():
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            tokens = line.split()
        cmd: list[str] = []
        for t in tokens + [";"]:
            if t in ("&&", "||", ";", "|"):
                if cmd:
                    out.append(cmd)
                cmd = []
            else:
                cmd.append(t)
    return out


def _opt(cmd: list[str], names: tuple[str, ...]) -> list[str]:
    vals = []
    for i, t in enumerate(cmd):
        if t in names and i + 1 < len(cmd):
            vals.append(cmd[i + 1])
        for n in names:
            if n.startswith("--") and t.startswith(n + "="):
                vals.append(t[len(n) + 1:])
    return vals


def _positionals(args: list[str]) -> list[str]:
    out, skip = [], False
    for t in args:
        if skip:
            skip = False
        elif t.startswith("-"):
            skip = t in _VALUE_OPTS
        else:
            out.append(t)
    return out


def _after(cmd: list[str], words: tuple[str, ...]) -> list[str] | None:
    """명령 토큰에서 words가 연이어 나온 다음 토큰들(없으면 None)."""
    n = len(words)
    for i in range(len(cmd) - n + 1):
        if tuple(cmd[i:i + n]) == words:
            return cmd[i + n:]
    return None


@dataclass
class _Step:
    job: str
    index: int
    data: dict
    env: dict[str, str]
    workdir: str | None

    @property
    def run(self) -> str:
        return _subst(str(self.data.get("run") or ""), self.env)

    @property
    def with_(self) -> dict[str, str]:
        return {k: _subst(v, self.env) for k, v in _env_map(self.data.get("with")).items()}


def _builds(step: _Step) -> list[tuple[str | None, str | None, list[str]]]:
    """step의 docker 빌드: (컨텍스트 디렉터리, Dockerfile 경로, 태그들)."""
    out = []
    base = step.workdir or ""
    if fnmatchcase(str(step.data.get("uses") or "").split("@")[0], "docker/build-push-action"):
        w = step.with_
        ctx = _norm(w.get("context", "."), base)
        df = _norm(w["file"], base) if "file" in w else None if ctx is None else posixpath.join(ctx, "Dockerfile")
        tags = [t.strip() for t in re.split(r"[,\n]", w.get("tags", "")) if t.strip()]
        out.append((ctx, df, tags))
    for cmd in _commands(step.run):
        args = _after(cmd, ("docker", "build")) or _after(cmd, ("docker", "buildx", "build"))
        if args is None:
            continue
        pos = _positionals(args)
        ctx = _norm(pos[-1], base) if pos else None
        files = _opt(args, ("-f", "--file"))
        df = _norm(files[0], base) if files else None if ctx is None else posixpath.join(ctx, "Dockerfile")
        out.append((ctx, df, _opt(args, ("-t", "--tag"))))
    return out


def _by_build(builds, workloads: list[WorkloadInfo], artifacts: list[ParsedArtifact]) -> dict[str, str]:
    out = {}
    for ctx, df, _ in builds:
        for w in workloads:
            wdf = workload_dockerfile(w, artifacts)
            if (df is not None and wdf is not None and wdf.path == df) or (ctx is not None and w.code_root == ctx):
                out[w.id] = "confirmed"
    return out


def _by_dirs(dirs: list[tuple[str | None, str]], workloads: list[WorkloadInfo]) -> dict[str, str]:
    out = {}
    for d, status in dirs:
        for w in workloads:
            if d is not None and w.code_root == d:
                out.setdefault(w.id, status)
    return out


def _by_images(images: list[str], workloads: list[WorkloadInfo]) -> dict[str, str]:
    out = {}
    for img in images:
        name = image_name(img) if "$" not in img.split(":")[0] else ""
        for w in workloads if name else []:
            if w.image and image_name(w.image) == name:
                out[w.id] = "confirmed"
            elif w.name == name:
                out.setdefault(w.id, "candidate")
    return out


def _by_names(names: list[str], workloads: list[WorkloadInfo]) -> dict[str, str]:
    return {w.id: "candidate" for w in workloads if w.name in {n for n in names if "$" not in n}}


def _by_compose(files: list[str], mentions_compose: bool, environments: list[Environment]) -> dict[str, str]:
    composes = [e for e in environments if e.kind == "compose"]
    out: dict[str, str] = {}
    for f in files:
        exact = [e for e in composes if e.source == _norm(f)]
        named = [e for e in composes if PurePosixPath(e.source).name == PurePosixPath(f).name]
        envs, status = (exact, "confirmed") if exact else (named, "candidate") if len(named) == 1 else ([], "")
        for e in envs:
            for wid in e.members:
                out.setdefault(wid, status)
    if not files and mentions_compose:
        root = [e for e in composes if e.name == "compose"]
        for e in root:
            for wid in e.members:
                out.setdefault(wid, "candidate")
    return out


def _by_manifests(paths: list[str], workloads: list[WorkloadInfo], environments: list[Environment]) -> dict[str, str]:
    out = {}
    for p in paths:
        for e in environments:
            if e.kind == "kustomize" and parent_dir(e.source) == p:
                for w in workloads:
                    if workload_in(e, w):
                        out[w.id] = "confirmed"
        for w in workloads:
            ep = as_dict(w.entrypoint).get("path")
            if w.source == "k8s" and isinstance(ep, str) and _under(ep, p):
                out[w.id] = "confirmed"
    return out


def _targets(rule: dict, step: _Step, job_steps: list[_Step], all_steps: list[_Step], raw: str,
             workloads: list[WorkloadInfo], artifacts: list[ParsedArtifact],
             environments: list[Environment]) -> dict[str, str]:
    cmds = [c for c in _commands(step.run)]
    w = step.with_
    base = step.workdir or ""
    flat = [t for c in cmds for t in c]
    for method in rule["target"]:
        found: dict[str, str] = {}
        if method == "source":
            dirs = _opt(flat, ("--source",)) + ([w["source"]] if "source" in w else [])
            found = _by_dirs([(_norm(d, base), "confirmed") for d in dirs], workloads)
        elif method == "image":
            found = _by_images(_opt(flat, ("--image",)) + ([w["image"]] if "image" in w else []), workloads)
        elif method == "job-build":
            found = _by_build([b for s in job_steps for b in _builds(s)], workloads, artifacts)
        elif method == "workflow-build":
            found = _by_build([b for s in all_steps for b in _builds(s)], workloads, artifacts)
        elif method == "compose":
            files = sorted(set(_COMPOSE_FILE.findall(raw)))
            found = _by_compose(files, bool(_COMPOSE_CMD.search(raw)), environments)
        elif method == "cwd":
            dirs = [(_norm(d, base), "confirmed") for d in _opt(flat, ("--cwd",))]
            dirs += [(_norm(parent_dir(c), base), "confirmed") for c in _opt(flat, ("--config", "-c"))]
            if "working-directory" in w:
                dirs.append((_norm(w["working-directory"]), "confirmed"))
            if not dirs:
                dirs = [(base, "confirmed" if step.workdir is not None else "candidate")]
            found = _by_dirs(dirs, workloads)
        elif method == "manifest":
            paths = [p for c in cmds for p in _opt(_after(c, ("kubectl", "apply")) or [],
                                                   ("-f", "--filename", "-k", "--kustomize"))]
            for c in cmds:
                helm = _after(c, ("helm", "upgrade"))
                pos = _positionals(helm or [])
                if len(pos) >= 2:
                    paths.append(pos[1])
            found = _by_manifests([n for p in paths if (n := _norm(p, base)) is not None], workloads, environments)
        elif method == "name":
            names = _opt(flat, ("--service",)) + [w[k] for k in ("service", "container-name") if k in w]
            for c in cmds:
                run_args = _after(c, ("run", "deploy"))
                names += _positionals(run_args or [])[:1]
            found = _by_names(names, workloads)
        if found:
            return found
    return {}


def _job_steps(snap: Snapshot, art: ParsedArtifact, data: dict) -> list[_Step]:
    wf_env = _env_map(data.get("env"))
    out = []
    for job_name, job in as_dict(data.get("jobs")).items():
        job = as_dict(job)
        env = {**wf_env, **_env_map(job.get("env"))}
        default_dir = as_dict(as_dict(job.get("defaults")).get("run")).get("working-directory")
        steps = job.get("steps")
        for i, step in enumerate(steps if isinstance(steps, list) else []):
            step = as_dict(step)
            senv = {**env, **_env_map(step.get("env"))}
            workdir = step.get("working-directory", default_dir)
            s = _Step(str(job_name), i, step, senv,
                      _norm(workdir) if isinstance(workdir, str) else None)
            out.append(s)
            # 이 step이 정한 셸 변수·GITHUB_ENV 값은 같은 job의 다음 step이 쓴다
            for line in s.run.splitlines():
                for m in [_ASSIGN.match(line), *_GITHUB_ENV.finditer(line)]:
                    if m:
                        env[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def _rule_for(step: _Step) -> tuple[dict, str, re.Pattern | None] | None:
    uses = str(step.data.get("uses") or "").split("@")[0]
    run = str(step.data.get("run") or "")
    for rule in kb.deploy():
        m = rule["match"]
        if "uses" in m and uses and fnmatchcase(uses, m["uses"]):
            return rule, "uses", None
        if "run" in m and run and re.search(m["run"], run):
            return rule, "run", re.compile(m["run"])
    return None


def ci_targets(snap: Snapshot, artifacts: list[ParsedArtifact], workloads: list[WorkloadInfo],
               environments: list[Environment]) -> list[DeployTarget]:
    out: list[DeployTarget] = []
    candidates = _members(workloads)
    for art in sorted((a for a in artifacts if a.kind == "ci" and a.parsed), key=lambda a: a.path):
        data = as_dict(art.objects[0][1]) if art.objects and isinstance(art.objects[0], tuple) else {}
        steps = _job_steps(snap, art, data)
        found = [(s, r) for s in steps if (r := _rule_for(s))]
        if not found:
            continue
        manual = not (_triggers(data) & AUTO_TRIGGERS)
        nodes = _step_nodes(snap, art.path)
        stem = PurePosixPath(art.path).stem
        per_job = {s.job: sum(1 for x, _ in found if x.job == s.job) for s, _ in found}
        raw = snap.read(art.path)
        for step, (rule, key, rx) in found:
            name = f"ci/{stem}"
            if len(found) > 1:
                name += f"/{step.job}" + (f"/{step.index}" if per_job[step.job] > 1 else "")
            line = _step_line(snap, art.path, nodes.get((step.job, step.index)), key, rx)
            ev = evidence(snap, art.path, line)
            job_steps = [s for s in steps if s.job == step.job]
            members = _targets(rule, step, job_steps, steps, raw, candidates, artifacts, environments)
            env = Environment(name, art.path, True, kind="ci-deploy", members={wid: "" for wid in members},
                              manual=manual, source_line=line)
            by_id = {w.id: w for w in workloads}
            comps = [_compute_entry(by_id[wid], rule["compute"], rule.get("label"), None, name, status, [ev])
                     for wid, status in sorted(members.items())]
            unmapped = None if members else {"label": f"deploy-target:{rule['id']}", "evidence": [ev]}
            out.append(DeployTarget(env, comps, unmapped))
    return out


def detect_deploy_targets(snap: Snapshot, artifacts: list[ParsedArtifact], workloads: list[WorkloadInfo],
                          environments: list[Environment]) -> tuple[list[Environment], list[dict], list[dict]]:
    """(환경, compute 기록, 미매핑). 이름이 기존 환경과 겹치면 `@<원본 경로>`를 붙인다."""
    targets = platform_targets(snap, artifacts, workloads) + ci_targets(snap, artifacts, workloads, environments)
    taken = {e.name for e in environments}
    comps: list[dict] = []
    unmapped: dict[str, list[dict]] = {}
    for t in targets:
        if t.env.name in taken:
            new = f"{t.env.name}@{t.env.source}" + (f":{t.env.source_line}" if t.env.source_line else "")
            for c in t.components:
                c["environment"] = new
            t.env.name = new
        taken.add(t.env.name)
        comps += t.components
        if t.unmapped:
            evs = unmapped.setdefault(t.unmapped["label"], [])
            evs += [e for e in t.unmapped["evidence"] if e not in evs]
    envs = sorted((t.env for t in targets), key=lambda e: e.name)
    comps.sort(key=lambda c: (c["scope"], c["environment"]))
    return envs, comps, [{"label": k, "evidence": v} for k, v in sorted(unmapped.items())]
