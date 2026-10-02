"""사용자 프로젝트 소스 읽기.

Main Server가 S3에 저장한 소스 스냅샷(폴더 prefix 또는 zip/tar.gz)을 메모리로 읽어 온다.
이 모듈이 지키는 것:
- 경로 탈출 차단: 절대경로, `..` 이 들어간 항목은 버린다 (zip slip 방지)
- 크기 제한: 파일 개수·전체 용량 상한을 넘으면 거부
- 비밀 파일 차단: `.env`, 키 파일 등은 목록에는 보이지만 내용은 절대 돌려주지 않는다
- 무거운 폴더 제외: node_modules, .git 등
"""
import fnmatch
import io
import posixpath
import tarfile
import zipfile
from pathlib import Path

from .errors import AgentError

MAX_FILES = 5000
MAX_TOTAL_BYTES = 50 * 1024 * 1024        # 스냅샷 전체 50MB
MAX_READ_CHARS = 20_000                    # LLM에 한 번에 넘기는 파일 내용 상한

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".next", ".nuxt", "target", ".idea", ".vscode", ".pytest_cache", ".terraform"}
SECRET_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                   "credentials", "*.tfstate", "*.tfstate.*", "secrets.*", "*.keystore"]
BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz", ".tar",
               ".jar", ".class", ".so", ".dll", ".exe", ".bin", ".onnx", ".pt", ".pkl", ".woff", ".woff2"}


def normalize(path: str) -> str | None:
    """안전한 상대경로로 바꾼다. 위험한 경로면 None."""
    p = path.replace("\\", "/").strip()
    if not p or p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return None
    p = posixpath.normpath(p)
    if p == "." or p.startswith("../") or p == ".." or "/../" in f"/{p}/":
        return None
    return p


def is_secret(path: str) -> bool:
    name = posixpath.basename(path).lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS)


def _skipped(path: str) -> bool:
    return any(part in SKIP_DIRS for part in path.split("/")[:-1])


class SourceTree:
    """프로젝트 파일을 {상대경로: bytes}로 들고 있는 읽기 전용 객체."""

    def __init__(self, files: dict[str, bytes]):
        self._files = {}
        total = 0
        for raw, data in files.items():
            p = normalize(raw)
            if p is None or _skipped(p):
                continue
            total += len(data)
            if len(self._files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise AgentError("source_too_large",
                                 f"프로젝트가 너무 큽니다 (최대 {MAX_FILES}개, {MAX_TOTAL_BYTES // 1024 // 1024}MB)")
            self._files[p] = data
        self._files = _strip_common_root(self._files)
        if not self._files:
            raise AgentError("source_empty", "읽을 수 있는 파일이 없습니다")

    # ---- 조회 ----
    def paths(self) -> list[str]:
        return sorted(self._files)

    def exists(self, path: str) -> bool:
        p = normalize(path)
        return p is not None and p in self._files

    def size(self, path: str) -> int:
        return len(self._files.get(normalize(path) or "", b""))

    def read_text(self, path: str, limit: int = MAX_READ_CHARS) -> str:
        """파일 내용을 텍스트로. 비밀·바이너리·없는 파일은 AgentError."""
        p = normalize(path)
        if p is None or p not in self._files:
            raise AgentError("file_not_found", f"파일이 없습니다: {path}")
        if is_secret(p):
            raise AgentError("secret_file", f"비밀 파일은 읽을 수 없습니다: {p}")
        if posixpath.splitext(p)[1].lower() in BINARY_EXTS:
            raise AgentError("binary_file", f"바이너리 파일입니다: {p}")
        text = self._files[p].decode("utf-8", errors="replace")
        if len(text) > limit:
            text = text[:limit] + f"\n... (이하 생략, 전체 {len(text)}자)"
        return text

    def lines(self, path: str) -> list[str]:
        p = normalize(path)
        if p is None or p not in self._files or is_secret(p):
            return []
        return self._files[p].decode("utf-8", errors="replace").splitlines()


def _strip_common_root(files: dict[str, bytes]) -> dict[str, bytes]:
    """GitHub zip처럼 `repo-sha/` 폴더 하나로 감싸져 있으면 그 껍데기를 벗긴다."""
    if not files:
        return files
    firsts = {p.split("/", 1)[0] for p in files}
    if len(firsts) == 1 and all("/" in p for p in files):
        root = firsts.pop() + "/"
        return {p[len(root):]: d for p, d in files.items()}
    return files


# ---- 불러오기 ----

def load(uri: str, s3_client=None) -> SourceTree:
    """s3://버킷/경로(/ 로 끝나면 폴더, .zip/.tar.gz면 압축) 또는 로컬 경로."""
    if uri.startswith("s3://"):
        return _load_s3(uri, s3_client)
    path = Path(uri)
    if path.is_dir():
        return SourceTree({f.relative_to(path).as_posix(): f.read_bytes()
                           for f in path.rglob("*") if f.is_file()
                           and not _skipped(f.relative_to(path).as_posix())})
    if path.is_file():
        return SourceTree(_unpack(path.name, path.read_bytes()))
    raise AgentError("source_not_found", f"소스를 찾을 수 없습니다: {uri}")


def _unpack(name: str, blob: bytes) -> dict[str, bytes]:
    if len(blob) > MAX_TOTAL_BYTES:
        raise AgentError("source_too_large", "압축 파일이 너무 큽니다")
    files: dict[str, bytes] = {}
    lower = name.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for info in z.infolist():
                    if not info.is_dir() and info.file_size <= MAX_TOTAL_BYTES:
                        files[info.filename] = z.read(info)
        elif lower.endswith((".tar.gz", ".tgz", ".tar")):
            with tarfile.open(fileobj=io.BytesIO(blob)) as t:
                for m in t.getmembers():
                    if m.isfile() and m.size <= MAX_TOTAL_BYTES:   # 링크·장치 파일은 무시
                        f = t.extractfile(m)
                        if f:
                            files[m.name] = f.read()
        else:
            raise AgentError("source_format", f"지원하지 않는 압축 형식입니다: {name}")
    except (zipfile.BadZipFile, tarfile.TarError) as e:
        raise AgentError("source_format", f"압축을 풀 수 없습니다: {e}") from None
    return files


def _split_s3(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket:
        raise AgentError("bad_request", f"S3 주소가 올바르지 않습니다: {uri}")
    return bucket, key


def _load_s3(uri: str, s3) -> SourceTree:
    import boto3
    from botocore.exceptions import ClientError
    from . import config
    s3 = s3 or boto3.client("s3", region_name=config.REGION)
    bucket, key = _split_s3(uri)
    try:
        if key.endswith("/") or key == "":
            files: dict[str, bytes] = {}
            total = 0
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key):
                for obj in page.get("Contents", []):
                    rel = obj["Key"][len(key):]
                    if not rel or _skipped(normalize(rel) or ".git/x"):
                        continue
                    total += obj["Size"]
                    if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                        raise AgentError("source_too_large", "프로젝트가 너무 큽니다")
                    files[rel] = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            return SourceTree(files)
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return SourceTree(_unpack(key, body))
    except ClientError as e:
        raise AgentError("source_unavailable", f"S3에서 소스를 읽지 못했습니다: {e.response['Error']['Code']}") from None
