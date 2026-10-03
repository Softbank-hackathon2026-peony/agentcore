"""S3 download concurrency preserves bytes, limits, and failure handling."""
import io
import threading

import pytest
from botocore.exceptions import ClientError

from agent import source
from agent.errors import AgentError


class FakeS3:
    def __init__(self, objects, barrier=None, fail=False):
        self.objects, self.barrier, self.fail = objects, barrier, fail
        self.calls, self.bodies = [], []

    def get_paginator(self, name):
        return self

    def paginate(self, **kwargs):
        yield {"Contents": [{"Key": k, "Size": len(v)} for k, v in self.objects.items()]}

    def get_object(self, *, Bucket, Key):
        self.calls.append(Key)
        if self.barrier:
            self.barrier.wait(timeout=5)
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
        body = io.BytesIO(self.objects[Key])
        self.bodies.append(body)
        return {"Body": body}


def test_s3_reads_concurrently_preserving_content_and_secret_protection():
    objects = {f"src/file{i}.py": f"print({i})".encode() for i in range(7)}
    objects["src/.env"] = b"TOKEN=secret"
    objects["src/.git/config"] = b"ignored"
    client = FakeS3(objects, threading.Barrier(8))
    tree = source.load("s3://test/src/", client)
    assert tree._files == {k[4:]: v for k, v in objects.items() if ".git/" not in k}
    assert len(client.calls) == 8 and all(b.closed for b in client.bodies)
    with pytest.raises(AgentError, match="비밀 파일"):
        tree.read_text(".env")


def test_s3_rejects_oversized_listing_before_download(monkeypatch):
    monkeypatch.setattr(source, "MAX_TOTAL_BYTES", 2)
    client = FakeS3({"src/a.py": b"aaa"})
    with pytest.raises(AgentError) as exc:
        source.load("s3://test/src/", client)
    assert exc.value.code == "source_too_large"
    assert client.calls == []


def test_s3_download_failure_keeps_agent_error():
    client = FakeS3({"src/a.py": b"a"}, fail=True)
    with pytest.raises(AgentError) as exc:
        source.load("s3://test/src/", client)
    assert exc.value.code == "source_unavailable"
