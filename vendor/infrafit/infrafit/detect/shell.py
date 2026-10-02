"""셸 명령 문자열 나누기: 실행 명령(앱 서버 판정)과 Dockerfile RUN(패키지 설치 읽기)이 함께 쓴다."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

_SHELLS = {"sh", "bash", "ash", "dash", "zsh"}
_COMMAND_PREFIXES = {"exec", "npx"}
_SEPARATORS = {"&&", ";", "||", "&", "|"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_PYTHON = re.compile(r"^python[\d.]*$")


def shell_tokens(command: str) -> list[str]:
    """셸 규칙으로 나눈 낱말(`&&`·`;`·`||`·`&`·`|`는 따로 떼어 낸다). 따옴표가 깨졌으면 공백으로만 나눈다."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return command.split()


def simple_commands(tokens: list[str], depth: int = 0) -> list[list[str]]:
    """명령 낱말들 → 단순 명령들(실행 순서). `&&`·`;`·`||`·`&`(백그라운드)·`|`(파이프)로 나누고, 앞의 환경 변수 대입과 `exec`·`npx`를 떼고,
    `sh -c "..."`·`bash -c ...`는 그 안의 명령으로, `python -m <모듈>`은 모듈 명령으로 바꾼다."""
    out: list[list[str]] = []
    group: list[str] = []
    for token in tokens + [";"]:
        if token not in _SEPARATORS:
            group.append(token)
            continue
        while group and (group[0] in _COMMAND_PREFIXES or _ENV_ASSIGN.match(group[0])):
            group = group[1:]
        if group and PurePosixPath(group[0]).name in _SHELLS and "-c" in group and depth < 5:
            rest = group[group.index("-c") + 1:]
            out += simple_commands(shell_tokens(rest[0]) if len(rest) == 1 else rest, depth + 1)
        elif group and _PYTHON.match(PurePosixPath(group[0]).name) and group[1:2] == ["-m"]:
            out += simple_commands(group[2:], depth + 1)
        elif group:
            out.append(group)
        group = []
    return out
