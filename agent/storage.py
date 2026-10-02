"""결과물 저장 (S3 또는 로컬 폴더).

시도마다 새 경로에 저장하고 덮어쓰지 않는다. 그래야 자동 수정 루프에서 무엇이 바뀌었는지 추적된다.
경로 규칙:
  projects/<project_id>/analysis/<analysis_id>/recommendation.json
  projects/<project_id>/build/<analysis_id>/attempt-<N>/{Dockerfile.pawploy, dockerignore, buildspec.yml}
  projects/<project_id>/deploy/<deploy_id>/attempt-<N>/<aws|gcp>/{main.tf, ...}
"""
import json
from pathlib import Path

from . import config
from .errors import AgentError


class Store:
    def put_text(self, key: str, text: str, content_type: str = "text/plain") -> str | None:
        raise NotImplementedError

    def put_json(self, key: str, data: dict) -> str | None:
        return self.put_text(key, json.dumps(data, ensure_ascii=False, indent=2), "application/json")

    def prefix_uri(self, key_prefix: str) -> str | None:
        raise NotImplementedError

    def subdirs(self, key_prefix: str) -> list[str]:
        """key_prefix 바로 아래 폴더 이름들 (끝의 / 없이)."""
        return []

    def list_keys(self, key_prefix: str) -> list[str]:
        """key_prefix 아래 모든 파일 키."""
        return []

    def get_text(self, key: str) -> str:
        raise AgentError("store_failed", f"읽을 수 없습니다: {key}")


class NullStore(Store):
    """버킷이 설정되지 않았을 때: 저장하지 않고 응답에만 담는다."""

    def put_text(self, key, text, content_type="text/plain"):
        return None

    def prefix_uri(self, key_prefix):
        return None


class LocalStore(Store):
    """테스트·로컬 실행용."""

    def __init__(self, root: str):
        self.root = Path(root)

    def put_text(self, key, text, content_type="text/plain"):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path.as_posix()

    def prefix_uri(self, key_prefix):
        return (self.root / key_prefix).as_posix().rstrip("/") + "/"

    def subdirs(self, key_prefix):
        d = self.root / key_prefix
        return sorted(x.name for x in d.iterdir() if x.is_dir()) if d.is_dir() else []

    def list_keys(self, key_prefix):
        d = self.root / key_prefix
        return sorted(f.relative_to(self.root).as_posix() for f in d.rglob("*") if f.is_file()) if d.is_dir() else []

    def get_text(self, key):
        return (self.root / key).read_text(encoding="utf-8")


class S3Store(Store):
    def __init__(self, bucket: str, client=None):
        import boto3
        self.bucket = bucket
        self.s3 = client or boto3.client("s3", region_name=config.REGION)

    def put_text(self, key, text, content_type="text/plain"):
        from botocore.exceptions import ClientError
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"),
                               ContentType=content_type + "; charset=utf-8")
        except ClientError as e:
            raise AgentError("store_failed", f"S3 저장 실패: {e.response['Error']['Code']}") from None
        return f"s3://{self.bucket}/{key}"

    def prefix_uri(self, key_prefix):
        return f"s3://{self.bucket}/{key_prefix.rstrip('/')}/"

    def subdirs(self, key_prefix):
        prefix = key_prefix.rstrip("/") + "/"
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            out += [c["Prefix"][len(prefix):].rstrip("/") for c in page.get("CommonPrefixes", [])]
        return out

    def list_keys(self, key_prefix):
        prefix = key_prefix.rstrip("/") + "/"
        return [o["Key"] for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
                for o in page.get("Contents", [])]

    def get_text(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")


def default_store() -> Store:
    b = config.ARTIFACT_BUCKET
    if not b:
        return NullStore()
    if b.startswith("local:"):
        return LocalStore(b[len("local:"):])
    return S3Store(b)
