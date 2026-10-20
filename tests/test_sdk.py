import io
import json
from pathlib import Path

import pytest

from metis_sdk import (
    Client,
    MetisError,
    ModelAttribution,
    context_from_headers,
    platform_identity_from_headers,
    platform_identity_from_request,
    with_external_user,
    with_platform_identity,
    without_user_attribution,
)


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
        if request.full_url.endswith("/endpoints/database"):
            payload = FIXTURE["endpoint"]
        elif request.full_url.endswith("/api/runtime/v1/config"):
            assert request.headers.get("X-metis-service") == "web"
            payload = {"schemaVersion": 1, "revision": 2, "config": {"settings": {"site-name": {"type": "string", "value": "demo"}}, "models": {"llm.0": {"slot": "llm.0", "model": "qwen-plus", "displayName": "通义千问 Plus", "supportsVision": True, "supportsTools": True, "activity": "active", "busyLevel": "unknown"}}, "storage": {}, "tenant": {}, "features": {}}}
        else:
            payload = FIXTURE["dependencies"]
        return Response(json.dumps(payload).encode())

    client = Client(environment={**FIXTURE["environment"], "METIS_SERVICE_NAME": "web"}, opener=open_request)
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
    runtime_config = client.runtime_config()
    assert runtime_config.revision == 2
    assert runtime_config.models["llm.0"].display_name == "通义千问 Plus"
    assert runtime_config.models["llm.0"].supports_vision is True
    llm = client.model("llm.0")
    assert llm.model == "example-chat"
    assert llm.supports_vision is True
    assert llm.supports_thinking is False
    assert llm.supports_tools is True
    assert llm.context_window == 32768
    assert llm.max_output_tokens == 8192

    assert client.try_model("llm.0") is not None
    assert client.try_model("llm.0").model == "example-chat"
    assert client.try_model("llm.1") is None
    assert client.try_model("invalid") is None

    llms = client.models("llm")
    assert len(llms) == 1
    assert llms[0].model == "example-chat"
    with pytest.raises(MetisError) as err:
        client.models("invalid")
    assert err.value.reason == "INVALID_CONFIG"

    emb = client.model("embedding.0")
    assert emb.model == "example-embedding"
    assert emb.dimensions == 1024
    assert emb.normalized is True

    rerank = client.model("rerank.0")
    assert rerank.model == "example-rerank"
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


def test_model_requests_always_carry_slot_key_and_one_attribution():
    model = Client(environment=FIXTURE["environment"]).model("llm.0")
    request = model.new_request("POST", "/v1/chat/completions", with_platform_identity(42, 84))
    assert request.get_header("Authorization") == "Bearer model-token"
    assert request.get_header("X-platform-tenant-id") == "42"
    assert request.get_header("X-platform-user-id") == "84"
    assert request.get_header("X-platform-external-user-id") is None

    request = model.new_request("POST", "/v1/embeddings", with_external_user(" customer-1 "))
    assert request.get_header("Authorization") == "Bearer model-token"
    assert request.get_header("X-platform-tenant-id") is None
    assert request.get_header("X-platform-user-id") is None
    assert request.get_header("X-platform-external-user-id") == "customer-1"

    request = model.new_request("POST", "/v1/rerank", without_user_attribution())
    assert request.get_header("Authorization") == "Bearer model-token"
    assert request.get_header("X-platform-external-user-id") is None


def test_model_attribution_rejects_invalid_values_and_paths():
    expected = with_platform_identity(42, 84)
    headers = {"X-Platform-Tenant-Id": "42", "x-platform-user-id": "84"}
    assert platform_identity_from_headers(headers) == expected
    assert platform_identity_from_request(type("Request", (), {"headers": headers})()) == expected
    for tenant_id, user_id in ((0, 1), (1, -1), (True, 1)):
        with pytest.raises(MetisError, match="positive integers"):
            with_platform_identity(tenant_id, user_id)
    for external_id in ("", " ", "a" * 129, "你" * 43, "bad\nvalue"):
        with pytest.raises(MetisError, match="1-128 bytes"):
            with_external_user(external_id)
    with pytest.raises(MetisError, match="valid platform identity"):
        platform_identity_from_headers({})

    model = Client(environment=FIXTURE["environment"]).model("llm.0")
    for path in ("", "v1/chat/completions", "//elsewhere.test/v1"):
        with pytest.raises(MetisError, match="absolute request path"):
            model.new_request("POST", path, without_user_attribution())
    with pytest.raises(MetisError, match="attribution is required"):
        model.new_request("POST", "/v1/chat/completions", ModelAttribution("invalid"))
