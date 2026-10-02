"""② 프로젝트 스캔 (LLM 없음).

정해진 규칙으로 사실만 뽑는다. LLM은 이 결과를 보고 판단하고, 필요하면 도구로 파일을 더 읽는다.
여기서 뽑은 근거(evidence)는 실제 파일·줄 번호를 가지므로, LLM이 단서를 지어냈는지 대조할 때도 쓴다.
"""
import json
import posixpath
import re

from .source import SourceTree, is_secret

LANG_BY_EXT = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
               ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript", ".go": "go",
               ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".rs": "rust",
               ".cs": "csharp", ".html": "html"}

# 의존성 이름 → (분류, 표시 이름)
PY_FRAMEWORKS = {"flask": "flask", "fastapi": "fastapi", "django": "django", "streamlit": "streamlit",
                 "gradio": "gradio", "uvicorn": "uvicorn", "gunicorn": "gunicorn", "aiohttp": "aiohttp",
                 "sanic": "sanic", "starlette": "starlette"}
JS_FRAMEWORKS = {"express": "express", "next": "nextjs", "@nestjs/core": "nestjs", "fastify": "fastify",
                 "koa": "koa", "hono": "hono", "nuxt": "nuxt", "vite": "vite", "react": "react",
                 "vue": "vue", "svelte": "svelte", "socket.io": "socket.io", "ws": "ws"}
DATASTORES = {"sqlite": "sqlite", "sqlite3": "sqlite", "psycopg": "postgres", "psycopg2": "postgres",
              "psycopg2-binary": "postgres", "asyncpg": "postgres", "pg": "postgres", "mysql": "mysql",
              "mysql2": "mysql", "pymysql": "mysql", "redis": "redis", "ioredis": "redis",
              "pymongo": "mongodb", "mongoose": "mongodb", "mongodb": "mongodb", "sqlalchemy": "sql",
              "prisma": "sql", "@prisma/client": "sql", "typeorm": "sql", "sequelize": "sql"}
REALTIME = {"socket.io", "ws", "websockets", "flask-socketio", "channels", "python-socketio"}
BACKGROUND = {"celery", "apscheduler", "schedule", "rq", "bull", "bullmq", "node-cron", "agenda"}

PORT_PATTERNS = [
    (re.compile(r"\.listen\(\s*(\d{2,5})"), "listen"),
    (re.compile(r"\bport\s*[=:]\s*(\d{2,5})", re.I), "port="),
    (re.compile(r"--port[ =](\d{2,5})"), "--port"),
    (re.compile(r"^\s*EXPOSE\s+(\d{2,5})", re.I), "EXPOSE"),
    (re.compile(r"PORT['\"]?\s*,\s*['\"]?(\d{2,5})"), "PORT 기본값"),
    (re.compile(r"PORT\s*\|\|\s*(\d{2,5})"), "PORT 기본값"),
]
ENV_PATTERNS = [
    re.compile(r"os\.environ\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"process\.env\.([A-Z][A-Z0-9_]*)"),
    re.compile(r"process\.env\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
]
ENV_EXAMPLE_FILES = {".env.example", ".env.sample", ".env.template", "env.example"}
CODE_EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".rb", ".php"}
MAX_SCAN_FILES = 400


def scan(src: SourceTree) -> dict:
    paths = src.paths()
    evidence: list[dict] = []

    def ev(path: str, line: int, text: str, kind: str):
        if len(evidence) < 60:
            evidence.append({"file": path, "line": line, "text": text.strip()[:160], "kind": kind})

    # 언어
    langs: dict[str, int] = {}
    for p in paths:
        lang = LANG_BY_EXT.get(posixpath.splitext(p)[1].lower())
        if lang:
            langs[lang] = langs.get(lang, 0) + 1

    deps: dict[str, str] = {}          # 의존성 이름 → 발견 파일
    start_scripts: dict[str, str] = {}

    # Python 의존성
    for p in [x for x in paths if posixpath.basename(x) in ("requirements.txt", "pyproject.toml", "Pipfile")]:
        for i, line in enumerate(src.lines(p), 1):
            m = re.match(r"\s*['\"]?([A-Za-z0-9_.\-\[\]]+)", line)
            if not m or line.strip().startswith("#"):
                continue
            name = re.sub(r"\[.*\]", "", m.group(1)).lower()
            if name in PY_FRAMEWORKS or name in DATASTORES or name in REALTIME or name in BACKGROUND:
                deps.setdefault(name, p)
                ev(p, i, line, "dependency")

    # Node 의존성
    for p in [x for x in paths if posixpath.basename(x) == "package.json"]:
        try:
            pkg = json.loads("\n".join(src.lines(p)))
        except ValueError:
            continue
        for section in ("dependencies", "devDependencies"):
            for name in (pkg.get(section) or {}):
                if name in JS_FRAMEWORKS or name in DATASTORES or name in REALTIME or name in BACKGROUND:
                    deps.setdefault(name, p)
                    ev(p, _line_of(src, p, f'"{name}"'), f'"{name}" ({section})', "dependency")
        for k, v in (pkg.get("scripts") or {}).items():
            if k in ("start", "dev", "serve", "build"):
                start_scripts[f"{p}:{k}"] = str(v)[:120]

    frameworks = sorted({PY_FRAMEWORKS.get(d) or JS_FRAMEWORKS.get(d) for d in deps
                         if d in PY_FRAMEWORKS or d in JS_FRAMEWORKS})
    datastores = sorted({DATASTORES[d] for d in deps if d in DATASTORES})

    # 다른 언어 빌드 파일
    build_files = [p for p in paths if posixpath.basename(p) in
                   ("go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "Cargo.toml")]

    # 포트·환경변수·실시간·백그라운드 단서 (코드 파일만, 상한)
    ports: list[dict] = []
    env_names: set[str] = set()
    code_files = [p for p in paths if posixpath.splitext(p)[1].lower() in CODE_EXTS or
                  posixpath.basename(p) in ("Dockerfile", "Procfile")][:MAX_SCAN_FILES]
    for p in code_files:
        for i, line in enumerate(src.lines(p), 1):
            for pat, how in PORT_PATTERNS:
                m = pat.search(line)
                if m and 1 <= int(m.group(1)) <= 65535:
                    ports.append({"port": int(m.group(1)), "file": p, "line": i, "how": how})
                    ev(p, i, line, "port")
                    break
            for pat in ENV_PATTERNS:
                for name in pat.findall(line):
                    env_names.add(name)
            low = line.lower()
            if "cron" in low or "setinterval(" in low or "schedule.every" in low:
                ev(p, i, line, "schedule")

    # .env.example 류: 이름만 (값은 안 봄)
    for p in paths:
        if posixpath.basename(p).lower() in ENV_EXAMPLE_FILES:
            for line in _raw_lines(src, p):
                m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
                if m:
                    env_names.add(m.group(1))

    dockerfiles = [p for p in paths if posixpath.basename(p) == "Dockerfile" or p.endswith(".Dockerfile")]
    compose = [p for p in paths if posixpath.basename(p) in
               ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")]
    compose_services = _compose_services(src, compose[0]) if compose else []
    k8s = sorted({p.split("/")[0] for p in paths if p.endswith((".yaml", ".yml")) and
                  _looks_like_k8s(src, p)})[:5]
    has_index_html = any(posixpath.basename(p) == "index.html" for p in paths)
    server_like = bool(set(frameworks) - {"react", "vue", "svelte", "vite"}) or bool(build_files)
    secrets_present = [p for p in paths if is_secret(p)]

    warnings = []
    if len(compose_services) > 1:
        warnings.append(f"docker compose 서비스가 {len(compose_services)}개입니다 ({', '.join(compose_services[:6])}). "
                        "컨테이너 1개 배포만 지원합니다.")
    if k8s:
        warnings.append("Kubernetes 매니페스트가 있습니다. 쿠버네티스 배포는 지원하지 않습니다.")
    if datastores and datastores != ["sqlite"]:
        warnings.append(f"외부 데이터베이스/캐시가 필요해 보입니다: {', '.join(datastores)}")
    if secrets_present:
        warnings.append(f"비밀 파일이 포함돼 있습니다 (내용은 읽지 않음): {', '.join(secrets_present[:5])}")

    return {
        "file_count": len(paths),
        "total_bytes": sum(src.size(p) for p in paths),
        "languages": dict(sorted(langs.items(), key=lambda x: -x[1])),
        "frameworks": frameworks,
        "datastores": datastores,
        "realtime": sorted(d for d in deps if d in REALTIME),
        "background_jobs": sorted(d for d in deps if d in BACKGROUND),
        "build_files": build_files,
        "start_scripts": start_scripts,
        "port_hints": ports[:10],
        "env_names": sorted(env_names)[:50],
        "dockerfiles": dockerfiles,
        "compose_services": compose_services,
        "k8s_dirs": k8s,
        "static_site": has_index_html and not server_like,
        "secret_files": secrets_present,
        "warnings": warnings,
        "evidence": evidence,
        "tree": _tree_preview(paths),
    }


def _line_of(src: SourceTree, path: str, needle: str) -> int:
    for i, line in enumerate(src.lines(path), 1):
        if needle in line:
            return i
    return 1


def _raw_lines(src: SourceTree, path: str) -> list[str]:
    # .env.example은 is_secret에 걸리므로 내부 데이터에서 직접 읽는다 (이름만 뽑는 용도)
    data = src._files.get(path, b"")  # noqa: SLF001 — 같은 패키지 내부 접근
    return data.decode("utf-8", errors="replace").splitlines()


def _compose_services(src: SourceTree, path: str) -> list[str]:
    services, inside = [], False
    for line in src.lines(path):
        if re.match(r"^services:\s*$", line):
            inside = True
            continue
        if inside:
            if re.match(r"^\S", line):
                break
            m = re.match(r"^  ([A-Za-z0-9_.\-]+):\s*$", line)
            if m:
                services.append(m.group(1))
    return services


def _looks_like_k8s(src: SourceTree, path: str) -> bool:
    head = "\n".join(src.lines(path)[:30])
    return "apiVersion:" in head and "kind:" in head


def _tree_preview(paths: list[str], limit: int = 120) -> list[str]:
    if len(paths) <= limit:
        return paths
    return paths[:limit] + [f"... 외 {len(paths) - limit}개"]
