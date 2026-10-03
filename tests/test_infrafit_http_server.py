"""A stdlib HTTP server loop is a web workload, not a background worker."""
from pathlib import Path

import pytest


@pytest.mark.parametrize("text, expected", [
    ("from http.server import ThreadingHTTPServer\nThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()", True),
    ("from http.server import HTTPServer as Server\nServer(('0.0.0.0', 8080), Handler).serve_forever()", True),
    ("from http.server import HTTPServer\nworker.serve_forever()", False),
    ("# from http.server import HTTPServer\n# HTTPServer(('', 80), Handler)", False),
    ("from fake import HTTPServer\nHTTPServer(('', 80), Handler).serve_forever()", False),
    ("while True:\n    consume()", False),
])
def test_http_server_requires_real_stdlib_import_and_call(monkeypatch, text, expected):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "vendor/infrafit"))
    from infrafit.detect.workloads import _stdlib_http_server
    assert _stdlib_http_server(text) is expected


def test_clean_http_fixture_is_web_in_inventory():
    from agent import source
    from agent.inventory import run_inventory
    root = Path(__file__).resolve().parents[1]
    inv = run_inventory(source.load(str(root / "evals/fixtures/clean_memo")))
    assert inv["status"] == "ok", inv
    assert [w["kind"] for w in inv["summary"]["workloads"]] == ["web"]
    assert inv["summary"]["recommendation"]["recommended"]["target"] in {"aws_lambda", "gcp_cloud_run"}
