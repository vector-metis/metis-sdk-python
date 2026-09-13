import io
import json
from pathlib import Path

import pytest

from metis_sdk import Client, MetisError, context_from_headers


FIXTURE = json.loads((Path(__file__).parents[1] / "testdata" / "runtime.json").read_text())


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def test_shared_contract_cache_refresh_and_config():
    calls = []

    def open_request(request):
        calls.append(request.full_url)
        payload = FIXTURE["endpoint"] if request.full_url.endswith("/endpoints/database") else FIXTURE["dependencies"]
        return Response(json.dumps(payload).encode())

    client = Client(environment=FIXTURE["environment"], opener=open_request)
    assert client.list_dependencies()[0]["alias"] == "ui"
    assert client.list_dependencies()[0]["requested_version"] == "^1.2.0"
    assert client.list_dependencies()[0]["resolved_version"] == "1.4.2"
    assert client.list_dependencies()[0]["package_sha256"] == "sha256:web"
    assert client.list_dependencies()[0]["direct"] is True
    client.list_dependencies()
    assert len(calls) == 1
    client.list_dependencies(refresh=True)
    assert len(calls) == 2
    assert client.service_endpoint("data", "database")["port"] == 31001
    assert client.model("llm.0")["model"] == "example-chat"
    assert client.model("embedding.0")["model"] == "example-embedding"
    assert client.model("embedding.0")["values"]["DIMENSIONS"] == "1024"
    assert client.model("rerank.0")["model"] == "example-rerank"
    assert client.model("rerank.0")["values"]["MAX_DOCUMENTS"] == "64"
    assert client.object_storage()["shared_buckets"] == ["shared-assets"]
    assert context_from_headers(FIXTURE["trustedHeaders"]).tenant_id == "42"

    for path in ("../admin", "%2e%2e/admin", "..\\admin"):
        with pytest.raises(MetisError) as path_error:
            client.web_url("ui", path)
        assert path_error.value.reason == "INVALID_CONFIG"


def test_explicit_local_config_and_missing_config():
    client = Client("http://localhost:80", "local-app", "token", environment={})
    assert client.app_id == "local-app"
    with pytest.raises(MetisError, match="required"):
        Client(environment={})


def test_invalid_runtime_and_capability_values_use_stable_errors():
    def open_request(_request):
        return Response(b"gateway failed")

    client = Client(
        "http://platform.example.invalid",
        "caller-a7x2m",
        "token",
        opener=open_request,
        environment={
            "METIS_S3_ENDPOINT": "http://storage.example.invalid",
            "METIS_S3_ACCESS_KEY": "key",
            "METIS_S3_SECRET_KEY": "secret",
            "METIS_S3_BUCKET": "bucket",
            "METIS_S3_SHARED_BUCKETS": "null",
        },
    )
    with pytest.raises(MetisError) as runtime_error:
        client.list_dependencies()
    assert runtime_error.value.reason == "UPSTREAM_FAILURE"
    with pytest.raises(MetisError) as model_error:
        client.model("llm.-1")
    assert model_error.value.reason == "INVALID_CONFIG"
    with pytest.raises(MetisError) as storage_error:
        client.object_storage()
    assert storage_error.value.reason == "INVALID_CONFIG"
