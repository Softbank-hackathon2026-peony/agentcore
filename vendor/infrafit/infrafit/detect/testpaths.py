"""테스트 코드 경로 판정: 엔드포인트 추출·시그니처 code 조건·개발용 Dockerfile 판정이 함께 쓴다."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

# 테스트 코드만 두는 디렉터리 조각(`src/test/`도 포함된다)
TEST_PATH_SEGMENTS = frozenset({"test", "tests", "__tests__", "e2e", "spec", "testing"})
# 개발·테스트용 Dockerfile(배포하지 않는 이미지): 파일 이름의 변형 부분 조각
DEV_DOCKERFILE_PARTS = frozenset({"dev", "development", "test", "tests", "testing", "ci", "local", "debug", "e2e"})
# 테스트 파일 이름 패턴(대소문자 구분: `Contest.java`는 테스트가 아니다)
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py", "conftest.py", "*.test.*", "*.spec.*",
                      "*Test.java", "*Test.kt", "*Tests.java", "*Tests.kt")


def is_test_path(rel: str) -> bool:
    """디렉터리 조각이 테스트 디렉터리이거나 파일 이름이 테스트 파일 패턴인 저장소 경로."""
    p = PurePosixPath(rel)
    if set(p.parts[:-1]) & TEST_PATH_SEGMENTS:
        return True
    return any(fnmatchcase(p.name, pat) for pat in TEST_FILE_PATTERNS)


def is_test_dir(d: str) -> bool:
    """디렉터리 경로의 조각 중 테스트 디렉터리가 있다(그 아래 매니페스트·빌드 파일은 배포 대상이 아니다)."""
    return bool(set(PurePosixPath(d).parts) & TEST_PATH_SEGMENTS)


def is_dev_dockerfile(path: str) -> bool:
    """`Dockerfile.<x>`·`<x>.Dockerfile`·`<x>.dockerfile`의 x를 `.`·`-`·`_`로 나눈 조각이 개발·테스트용이거나,
    테스트 경로(is_test_path)에 있는 Dockerfile."""
    p = PurePosixPath(path)
    name = p.name
    variant = ""
    if name.startswith("Dockerfile."):
        variant = name[len("Dockerfile."):]
    elif name.lower().endswith(".dockerfile"):
        variant = name[:-len(".dockerfile")]
    parts = set(re.split(r"[._-]", variant.lower())) if variant else set()
    return bool(parts & DEV_DOCKERFILE_PARTS) or is_test_path(path)
