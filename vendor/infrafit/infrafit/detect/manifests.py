"""의존성 매니페스트와 실행 정의 파싱."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from infrafit.detect.artifacts import is_dockerfile, parse_dockerfile
from infrafit.detect.jvm import read_gradle, read_maven
from infrafit.detect.shell import shell_tokens, simple_commands
from infrafit.detect.testpaths import is_dev_dockerfile
from infrafit.evidence import line_of
from infrafit.repo import Snapshot, parent_dir

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _norm_py(name: str) -> str:
    return name.lower().replace("_", "-")


@dataclass
class Manifests:
    deps: dict[str, tuple[str, int | None]] = field(default_factory=dict)
    deps_by_dir: dict[str, set[str]] = field(default_factory=dict)
    locations: dict[str, list[tuple[str, int | None]]] = field(default_factory=dict)
    scripts: dict[str, tuple[str, str, int | None]] = field(default_factory=dict)
    procfile: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    # (모듈 디렉터리, 의존성) → 그 모듈에 의존성을 준 (파일, 줄)들. 보통 모듈은 파일의 디렉터리이고, Gradle
    # subprojects·allprojects 블록의 의존성은 그 아래 하위 모듈들의 것이다
    module_locations: dict[tuple[str, str], list[tuple[str, int | None]]] = field(default_factory=dict)

    def add(self, name: str, rel: str, line: int | None, module: str | None = None) -> None:
        """module: 의존성이 속한 모듈 디렉터리(없으면 파일의 디렉터리)."""
        d = parent_dir(rel) if module is None else module
        self.add_location(name, rel, line)
        self.deps_by_dir.setdefault(d, set()).add(name)
        if (rel, line) not in self.module_locations.setdefault((d, name), []):
            self.module_locations[(d, name)].append((rel, line))

    def add_location(self, name: str, rel: str, line: int | None) -> None:
        """어느 모듈에도 속하지 않는 의존성 위치(시그니처 판정에만 쓴다)."""
        self.deps.setdefault(name, (rel, line))
        if (rel, line) not in self.locations.setdefault(name, []):
            self.locations[name].append((rel, line))


def _node(snap: Snapshot, m: Manifests) -> None:
    for rel in snap.glob("**/package.json"):
        try:
            data = json.loads(snap.read(rel))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        for section in ("dependencies", "devDependencies"):
            section_data = data.get(section)
            if not isinstance(section_data, dict):
                section_data = {}
            for name in sorted(section_data):
                m.add(name.lower(), rel, line_of(snap, rel, f'"{name}"'))
        scripts_data = data.get("scripts")
        if not isinstance(scripts_data, dict):
            scripts_data = {}
        for name, cmd in sorted(scripts_data.items()):
            m.scripts[f"{parent_dir(rel)}:{name}"] = (str(cmd), rel, line_of(snap, rel, f'"{name}"'))


def _requirements(snap: Snapshot, m: Manifests) -> None:
    for rel in snap.glob("**/requirements*.txt"):
        for i, text in enumerate(snap.lines(rel), 1):
            body = text.split("#", 1)[0].strip()
            if not body or body.startswith("-"):
                continue
            # Skip URLs, VCS references, paths
            if body.startswith(("git+", "hg+", "svn+", "bzr+", ".", "/")):
                continue
            # Skip lines with :// before @ or name (e.g., https://host/a.whl)
            scheme_end = body.split("@")[0]
            if "://" in scheme_end:
                continue
            # Handle PEP 508 direct references (name @ url)
            if " @ " in body:
                name = body.split(" @ ")[0].strip()
                m.add(_norm_py(name), rel, i)
                continue
            # Standard package requirement
            match = _REQ_NAME.match(body)
            if match:
                m.add(_norm_py(match.group(1)), rel, i)


def _pyproject(snap: Snapshot, m: Manifests) -> None:
    for rel in snap.glob("**/pyproject.toml"):
        try:
            data = tomllib.loads(snap.read(rel))
        except tomllib.TOMLDecodeError:
            continue
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        reqs = list(project.get("dependencies") or [])
        optional = project.get("optional-dependencies")
        for group in sorted(optional) if isinstance(optional, dict) else []:  # 선택 의존성 묶음 전부
            reqs += optional[group] if isinstance(optional[group], list) else []
        names = [mm.group(1) for r in reqs if (mm := _REQ_NAME.match(r))]
        poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
        names += [k for k in poetry if k.lower() != "python"]
        for name in names:
            m.add(_norm_py(name), rel, line_of(snap, rel, name))


def _procfile(snap: Snapshot, m: Manifests) -> None:
    for rel in snap.glob("**/Procfile"):
        for i, text in enumerate(snap.lines(rel), 1):
            if ":" not in text or text.lstrip().startswith("#"):
                continue
            proc, cmd = text.split(":", 1)
            m.procfile[f"{parent_dir(rel)}:{proc.strip()}"] = (cmd.strip(), rel, i)


# Dockerfile RUN의 패키지 설치 명령: 값을 받는 옵션(다음 낱말을 건너뛴다)
_PIP_VALUE_FLAGS = {"-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url",
                    "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix", "--root",
                    "--trusted-host", "--platform", "--python-version", "--src", "--cache-dir"}
_NODE_VALUE_FLAGS = {"--registry", "--prefix", "-w", "--workspace", "--filter", "--cache"}


def _install_args(tokens: list[str]) -> tuple[str, list[str]] | None:
    """단순 명령이 패키지 설치면 (python|node, 인자들). `pip install`·`uv pip install`·`npm install|i`·`yarn add`·
    `pnpm add`만 본다."""
    names = [PurePosixPath(t).name for t in tokens[:3]]
    if names[:1] in (["pip"], ["pip3"]) and names[1:2] == ["install"]:
        return "python", tokens[2:]
    if names[:3] == ["uv", "pip", "install"]:
        return "python", tokens[3:]
    if (names[:1] == ["npm"] and names[1:2] in (["install"], ["i"])) or names[:2] in (["yarn", "add"], ["pnpm", "add"]):
        return "node", tokens[2:]
    return None


def _package_name(kind: str, token: str) -> str | None:
    """설치 인자 → 패키지 이름(버전 고정·extras를 뗀다). 경로·URL·파일·변수는 None."""
    if not token or token.startswith((".", "/", "$", "~")) or "://" in token or ":" in token.split("@")[-1]:
        return None
    if kind == "python":
        if token.endswith((".whl", ".txt", ".tar.gz", ".zip")) or "/" in token:
            return None
        m = _REQ_NAME.match(token)
        return _norm_py(m.group(1)) if m else None
    scoped = token.startswith("@")
    name = ("@" if scoped else "") + token[1 if scoped else 0:].split("@")[0]
    return name.lower() if name.strip("@/") else None


def _dockerfile_installs(snap: Snapshot, m: Manifests) -> None:
    """Dockerfile `RUN`의 패키지 설치 이름(옵션·버전 고정을 뗀다). 근거는 그 RUN 줄. 모듈 의존성이 아니므로
    워크로드를 만들지 않고 위치만 남긴다(시그니처 판정). 전역 설치(`npm install -g` 등 도구)와 개발·테스트용
    Dockerfile(testpaths.is_dev_dockerfile)은 뺀다."""
    for rel in sorted(rel for rel in snap.files if is_dockerfile(rel) and not is_dev_dockerfile(rel)):
        for op, arg, line in parse_dockerfile(snap, rel).objects:
            if op != "RUN":
                continue
            try:
                tokens = [str(t) for t in json.loads(arg)] if arg.startswith("[") else shell_tokens(arg)
            except json.JSONDecodeError:
                tokens = shell_tokens(arg)
            for command in simple_commands(tokens):
                found = _install_args(command)
                if found is None:
                    continue
                kind, args = found
                if kind == "node" and ({"-g", "--global"} & set(args)):
                    continue
                value_flags = _PIP_VALUE_FLAGS if kind == "python" else _NODE_VALUE_FLAGS
                skip = False
                for token in args:
                    if skip:
                        skip = False
                    elif token.startswith("-"):
                        skip = token in value_flags
                    elif name := _package_name(kind, token):
                        m.add_location(name, rel, line)


def parse_manifests(snap: Snapshot) -> Manifests:
    m = Manifests()
    _node(snap, m)
    _requirements(snap, m)
    _pyproject(snap, m)
    _procfile(snap, m)
    read_gradle(snap, m)
    read_maven(snap, m)
    _dockerfile_installs(snap, m)
    return m
