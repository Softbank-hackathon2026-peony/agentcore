"""JVM(Java·Kotlin) 프로젝트: 소스 가리기·괄호 짝, Gradle·Maven 빌드 파일 의존성, Spring Boot 앱 모듈 찾기."""

from __future__ import annotations

import re
import tomllib
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from infrafit.detect.testpaths import is_test_path
from infrafit.evidence import evidence, line_at
from infrafit.repo import Snapshot, nearest_dir, parent_dir

if TYPE_CHECKING:
    from infrafit.detect.manifests import Manifests

_OPENERS = {"(": ")", "[": "]", "{": "}"}


def _blank_range(chars: list[str], start: int, end: int) -> None:
    for k in range(start, min(end, len(chars))):
        if chars[k] not in "\r\n":
            chars[k] = " "


def mask_code(text: str) -> tuple[str, str]:
    """Java·Kotlin·Gradle 소스 → (주석을 지운 글, 주석과 문자열 내용을 지운 글). 지운 글자는 공백이라
    위치와 줄 번호가 그대로다."""
    clean, struct = list(text), list(text)
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank_range(clean, i, j)
            _blank_range(struct, i, j)
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            _blank_range(clean, i, j)
            _blank_range(struct, i, j)
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
            _blank_range(struct, i + 3, j - 3)
        elif text[i] in "\"'":
            j = i + 1
            while j < n and text[j] not in (text[i], "\n"):
                j += 2 if text[j] == "\\" else 1
            j = min(j, n)
            _blank_range(struct, i + 1, j)
            j += 1
        else:
            i += 1
            continue
        i = max(j, i + 1)
    return "".join(clean), "".join(struct)


def close_bracket(struct: str, start: int) -> int:
    """struct[start]의 여는 괄호와 짝이 맞는 닫는 괄호 위치(괄호 종류는 가리지 않는다). 못 찾으면 글 끝."""
    depth = 0
    for k in range(start, len(struct)):
        if struct[k] in _OPENERS:
            depth += 1
        elif struct[k] in ")]}":
            depth -= 1
            if depth == 0:
                return k
    return len(struct)


SPRING_BOOT = "org.springframework.boot"  # Spring Boot 플러그인·parent를 뜻하는 의존성 이름
GRADLE_FILES = ("build.gradle", "build.gradle.kts")
MAVEN_FILE = "pom.xml"
_GRADLE_CONF = re.compile(r"(?<![\w.])(implementation|api|runtimeOnly|compileOnly|annotationProcessor|kapt"
                          r"|developmentOnly|testImplementation|testRuntimeOnly)\b")
_GRADLE_COORD = re.compile(r"""["']([\w.\-]+):([\w.\-]+)(?::[^"']*)?["']""")
_GRADLE_MAP = re.compile(r"""\bgroup\s*[:=]\s*["']([\w.\-]+)["']\s*,\s*name\s*[:=]\s*["']([\w.\-]+)["']""")
_GRADLE_ACCESSOR = re.compile(r"(?<![\w.])libs\.([\w.]+)")
_GRADLE_BOOT = re.compile(r"""\bid\s*\(?\s*["']org\.springframework\.boot["']"""
                          r"""|\bapply\s+plugin\s*:\s*["']org\.springframework\.boot["']"""
                          r"""|\bapply\s*\(\s*plugin\s*=\s*["']org\.springframework\.boot["']""")
_GRADLE_PLUGIN_ALIAS = re.compile(r"\balias\s*\(\s*libs\.plugins\.([\w.]+)\s*\)")
_GRADLE_APPLY_FALSE = re.compile(r"\bapply\s*\(?\s*false\b")
_GRADLE_SHARED = re.compile(r"(?<![\w.])(?:subprojects|allprojects"
                            r"|configure\s*\(\s*(?:subprojects|allprojects)\s*\))\s*\{")
XML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
VERSION_CATALOG = "gradle/libs.versions.toml"


def _alias_key(alias: str) -> str:
    """버전 카탈로그 별칭의 비교 키: `.`·`-`·`_`는 같은 구분자다(Gradle 접근자 규칙)."""
    return re.sub(r"[-_.]", ".", alias).lower()


def _catalog_coordinate(value) -> str | None:
    """[libraries] 값 → `g:a`: `"g:a:v"` 문자열, `{module = "g:a"}`, `{group, name}` 표."""
    if isinstance(value, str):
        parts = value.split(":")
        return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 and parts[0] and parts[1] else None
    if isinstance(value, dict):
        if isinstance(value.get("module"), str):
            return _catalog_coordinate(value["module"])
        if isinstance(value.get("group"), str) and isinstance(value.get("name"), str):
            return f"{value['group']}:{value['name']}"
    return None


def _catalog_plugin(value) -> str | None:
    """[plugins] 값 → 플러그인 id: `"id:v"` 문자열 또는 `{id = ...}` 표."""
    if isinstance(value, str):
        return value.split(":")[0] or None
    return value["id"] if isinstance(value, dict) and isinstance(value.get("id"), str) else None


def _version_catalog(snap: Snapshot, rel: str, cache: dict) -> tuple[dict[str, str], dict[str, str]]:
    """빌드 파일에서 위로 올라가며 처음 찾은 gradle/libs.versions.toml의 (라이브러리 별칭 → `g:a`,
    플러그인 별칭 → id). 없거나 깨졌으면 빈 사전들."""
    d = nearest_dir(rel, lambda d: snap.exists(f"{d}/{VERSION_CATALOG}" if d else VERSION_CATALOG))
    if d is None:
        return {}, {}
    path = f"{d}/{VERSION_CATALOG}" if d else VERSION_CATALOG
    if path not in cache:
        try:
            data = tomllib.loads(snap.read(path))
        except tomllib.TOMLDecodeError:
            data = {}
        libs = data.get("libraries") if isinstance(data.get("libraries"), dict) else {}
        plugins = data.get("plugins") if isinstance(data.get("plugins"), dict) else {}
        cache[path] = ({_alias_key(k): c for k, v in libs.items() if (c := _catalog_coordinate(v))},
                       {_alias_key(k): p for k, v in plugins.items() if (p := _catalog_plugin(v))})
    return cache[path]


def _conf_span(struct: str, end: int) -> tuple[int, int]:
    """구성 이름 뒤 인자 범위: `(`로 열면 짝 괄호까지(여러 줄 가능), 아니면 줄 끝까지."""
    m = re.match(r"\s*\(", struct[end:])
    if m:
        return end + m.end(), close_bracket(struct, end + m.end() - 1)
    stop = struct.find("\n", end)
    return end, len(struct) if stop < 0 else stop


def _shared_blocks(struct: str) -> list[tuple[int, int]]:
    """`subprojects { ... }`·`allprojects { ... }`·`configure(subprojects|allprojects) { ... }` 블록 범위들
    (하위 모듈에 적용하는 공통 설정)."""
    return [(m.start(), close_bracket(struct, m.end() - 1)) for m in _GRADLE_SHARED.finditer(struct)]


def read_gradle(snap: Snapshot, m: Manifests) -> None:
    """build.gradle(.kts)의 의존성 좌표(`g:a`, 버전 카탈로그 접근자 `libs.x` 포함)와 Spring Boot 플러그인
    (`alias(libs.plugins.x)` 포함). 주석은 지우고 읽는다. `test*` 구성과 `apply false`인 플러그인 선언(하위 모듈에만
    적용하는 루트 선언)은 건너뛴다. 줄은 좌표가 있는 줄.
    subprojects·allprojects 블록의 플러그인과 의존성은 이 파일의 모듈에 적용하지 않는다: 의존성은 그 아래에 빌드
    파일이 있는 하위 모듈마다 그 모듈의 것으로 두고(근거 줄은 이 파일), 플러그인은 버린다(라이브러리 모듈까지 앱으로
    만들지 않도록)."""
    catalogs: dict = {}
    rels = sorted(rel for pattern in GRADLE_FILES for rel in snap.glob(f"**/{pattern}"))
    dirs = sorted({parent_dir(rel) for rel in rels})
    for rel in rels:
        here = parent_dir(rel)
        children = [d for d in dirs if d != here and (not here or d.startswith(here + "/"))]
        libs, plugins = _version_catalog(snap, rel, catalogs)
        clean, struct = mask_code(snap.read(rel))
        shared = _shared_blocks(struct)
        shared_lines = {i for a, b in shared for i in range(line_at(clean, a), line_at(clean, b) + 1)}
        for i, text in enumerate(clean.splitlines(), 1):
            boot = _GRADLE_BOOT.search(text) or any(
                plugins.get(_alias_key(a)) == SPRING_BOOT for a in _GRADLE_PLUGIN_ALIAS.findall(text))
            if boot and not _GRADLE_APPLY_FALSE.search(text) and i not in shared_lines:
                m.add(SPRING_BOOT, rel, i)
        for conf in _GRADLE_CONF.finditer(struct):
            if conf.group(1).startswith("test"):
                continue
            start, stop = _conf_span(struct, conf.end())
            found = [(x.start(), f"{x.group(1)}:{x.group(2)}") for rx in (_GRADLE_COORD, _GRADLE_MAP)
                     for x in rx.finditer(clean, start, stop)]
            for x in _GRADLE_ACCESSOR.finditer(clean, start, stop):
                coord = libs.get(_alias_key(x.group(1).removesuffix(".get")))
                if coord:
                    found.append((x.start(), coord))
            in_shared = any(a <= conf.start() < b for a, b in shared)
            for pos, coord in sorted(found):
                # 하위 모듈이 없어도 저장소가 쓰는 의존성이므로 위치는 남긴다(시그니처 판정)
                m.add_location(coord.lower(), rel, line_at(clean, pos))
                for module in children if in_shared else [here]:
                    m.add(coord.lower(), rel, line_at(clean, pos), module)


def _blank(match: re.Match) -> str:
    """지운 부분을 같은 길이의 공백으로 바꾼다(줄바꿈은 남겨 위치와 줄 번호를 지킨다)."""
    return re.sub(r"[^\r\n]", " ", match.group(0))


def _xml_tag(block: str, tag: str) -> re.Match | None:
    return re.search(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", block)


def read_maven(snap: Snapshot, m: Manifests) -> None:
    """pom.xml의 `<dependency>`마다 `groupId:artifactId`(scope test 제외, 줄은 artifactId 줄).
    `<dependencyManagement>`(버전 선언)와 `<build>`(플러그인 의존성)의 항목은 앱 의존성이 아니므로 뺀다.
    parent가 spring-boot-starter-parent이면 org.springframework.boot(packaging이 pom인 집계 모듈은 제외)."""
    for rel in snap.glob(f"**/{MAVEN_FILE}"):
        masked = re.sub(r"<(dependencyManagement|build)>.*?</\1>", _blank,
                        XML_COMMENT.sub(_blank, snap.read(rel)), flags=re.DOTALL)
        for block in re.finditer(r"<dependency>(.*?)</dependency>", masked, re.DOTALL):
            body = block.group(1)
            group, artifact, scope = _xml_tag(body, "groupId"), _xml_tag(body, "artifactId"), _xml_tag(body, "scope")
            if not group or not artifact or (scope and scope.group(1) == "test"):
                continue
            m.add(f"{group.group(1)}:{artifact.group(1)}".lower(), rel,
                  line_at(masked, block.start(1) + artifact.start()))
        parent = re.search(r"<parent>(.*?)</parent>", masked, re.DOTALL)
        packaging = _xml_tag(masked, "packaging")
        artifact = _xml_tag(parent.group(1), "artifactId") if parent else None
        if artifact and artifact.group(1) == "spring-boot-starter-parent" and not (
                packaging and packaging.group(1) == "pom"):
            m.add(SPRING_BOOT, rel, line_at(masked, parent.start(1) + artifact.start()))


# Spring Boot 웹 스타터 → 프레임워크(둘 다 있으면 Spring Boot처럼 MVC가 먼저)
SPRING_WEB = (("org.springframework.boot:spring-boot-starter-web", "spring-mvc"),
              ("org.springframework.boot:spring-boot-starter-webflux", "spring-webflux"))
SPRING_STARTER = "org.springframework.boot:spring-boot-starter"
JVM_BUILD_FILES = GRADLE_FILES + (MAVEN_FILE,)
GRADLE_SETTINGS = ("settings.gradle", "settings.gradle.kts")
_BOOT_APPLICATION = re.compile(r"^\s*@(?:org\.springframework\.boot\.autoconfigure\.)?SpringBootApplication\b",
                               re.MULTILINE)
_ROOT_PROJECT = re.compile(r"""\brootProject\.name\s*=\s*["']([^"']+)["']""")


def spring_web(deps: set[str]) -> tuple[str, str] | None:
    """(웹 스타터 의존성, 프레임워크). Spring 웹 스타터가 없으면 None."""
    return next(((dep, fw) for dep, fw in SPRING_WEB if dep in deps), None)


def spring_boot_dep(deps: set[str]) -> str | None:
    """Spring Boot 앱의 근거 의존성: 플러그인·parent, 없으면 이름 순 첫 스타터."""
    if SPRING_BOOT in deps:
        return SPRING_BOOT
    return next((d for d in sorted(deps) if d.startswith(SPRING_STARTER)), None)


def jvm_build_dirs(snap: Snapshot) -> list[str]:
    """Gradle·Maven 빌드 파일이 있는 디렉터리들(경로 순)."""
    return sorted({parent_dir(rel) for name in JVM_BUILD_FILES for rel in snap.glob(f"**/{name}")})


def spring_name(snap: Snapshot, d: str) -> str:
    """settings.gradle(.kts)의 rootProject.name, pom의 프로젝트 artifactId, 없으면 디렉터리 이름(루트는 app)."""
    for name in GRADLE_SETTINGS:
        rel = f"{d}/{name}" if d else name
        m = _ROOT_PROJECT.search(snap.read(rel)) if snap.exists(rel) else None
        if m and m.group(1).strip():
            return m.group(1).strip()
    pom = f"{d}/{MAVEN_FILE}" if d else MAVEN_FILE
    if snap.exists(pom):
        # parent·의존성·빌드 블록의 artifactId를 빼고 남은 첫 artifactId가 프로젝트 자신이다
        text = XML_COMMENT.sub("", snap.read(pom))
        text = re.sub(r"<(parent|dependencies|dependencyManagement|build|profiles)>.*?</\1>", "", text, flags=re.DOTALL)
        m = re.search(r"<artifactId>\s*([^<]*?)\s*</artifactId>", text)
        if m and m.group(1):
            return m.group(1)
    return PurePosixPath(d).name or "app"


def build_location(manifests: Manifests, dep: str, d: str, files=JVM_BUILD_FILES) -> tuple[str, int | None] | None:
    """모듈 d에 dep를 준 빌드 파일의 (파일, 줄): d의 빌드 파일, 또는 위 모듈의 subprojects·allprojects 블록."""
    found = [(rel, ln) for rel, ln in manifests.module_locations.get((d, dep), []) if PurePosixPath(rel).name in files]
    return min(found, key=lambda loc: parent_dir(loc[0]) != d, default=None)  # d 자신의 빌드 파일이 먼저


def owning_build_dir(rel: str, build_dirs: set[str]) -> str | None:
    """파일을 품은 가장 가까운 빌드 파일 디렉터리(모듈). 없으면 None."""
    return nearest_dir(rel, build_dirs.__contains__)


def boot_application_dirs(snap: Snapshot, build_dirs: set[str]) -> set[str]:
    """테스트가 아닌 Java·Kotlin 파일에 `@SpringBootApplication`이 있는 모듈 디렉터리들."""
    out: set[str] = set()
    for pattern in ("**/*.java", "**/*.kt"):
        for rel in snap.glob(pattern):
            if not is_test_path(rel) and _BOOT_APPLICATION.search(snap.read(rel)):
                d = owning_build_dir(rel, build_dirs)
                if d is not None:
                    out.add(d)
    return out


def spring_apps(snap: Snapshot, manifests: Manifests) -> dict[tuple[str, str], dict]:
    """빌드 파일 디렉터리마다 Spring Boot 앱: 웹 스타터가 있으면 web, Spring Boot만 있으면 worker(후보).
    모듈 안 코드에 `@SpringBootApplication`이 있거나 Gradle Boot 플러그인을 적용한 모듈만 앱이다(스타터만 쓰는
    라이브러리 모듈은 뺀다)."""
    found: dict[tuple[str, str], dict] = {}
    build_dirs = jvm_build_dirs(snap)
    apps = boot_application_dirs(snap, set(build_dirs))
    for d in build_dirs:
        deps = manifests.deps_by_dir.get(d, set())
        web = spring_web(deps)
        dep = web[0] if web else spring_boot_dep(deps)
        if dep is None or (d not in apps and build_location(manifests, SPRING_BOOT, d, GRADLE_FILES) is None):
            continue
        loc = build_location(manifests, dep, d)
        if loc is None:
            continue
        found[("web" if web else "worker", d)] = {
            "spring": (evidence(snap, *loc), web[1] if web else "", spring_name(snap, d))}
    return found


