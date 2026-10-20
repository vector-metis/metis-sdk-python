"""只负责应用身份、依赖发现和能力环境解析的最小客户端。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


class MetisError(Exception):
    """保存平台稳定错误 reason 与 HTTP 状态码。"""

    def __init__(self, reason: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class RequestContext:
    """平台入口覆盖写入的可信调用上下文。"""

    tenant_id: str = ""
    user_id: str = ""
    role: str = ""
    source_app_id: str = ""
    actor_type: str = ""


@dataclass(frozen=True)
class ModelAttribution:
    """一次模型调用的最终用户归因；请通过模块辅助函数创建。"""

    kind: str
    tenant_id: int = 0
    user_id: int = 0
    external_user_id: str = ""


@dataclass(frozen=True)
class ModelConfig:
    """平台为一个模型 slot 注入的稳定网关配置。"""

    endpoint: str
    model: str
    api_key: str
    supports_vision: bool = False
    supports_thinking: bool = False
    supports_tools: bool = False
    context_window: int = 0
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    dimensions: int = 0
    normalized: bool = False

    def new_request(
        self,
        method: str,
        request_path: str,
        attribution: ModelAttribution,
        data: bytes | None = None,
    ) -> urllib.request.Request:
        """创建携带 slot API key 和显式最终用户归因的网关请求。"""

        endpoint = self.endpoint.strip().rstrip("/")
        request_path = request_path.strip()
        if not endpoint or not self.api_key or not request_path.startswith("/") or request_path.startswith("//"):
            raise MetisError("INVALID_CONFIG", "model endpoint, API key and absolute request path are required")
        headers = {"Authorization": "Bearer " + self.api_key}
        _apply_attribution(headers, attribution)
        try:
            return urllib.request.Request(endpoint + request_path, data=data, method=method, headers=headers)
        except ValueError as error:
            raise MetisError("INVALID_CONFIG", f"model request is invalid: {error}") from error


@dataclass(frozen=True)
class RuntimeModel:
    """Runtime API 返回的模型展示和能力投影，不包含任何凭据。"""

    slot: str
    slot_type: str
    slot_index: int
    interface: str
    endpoint: str
    model: str
    display_name: str
    icon_url: str
    description: str
    traits: tuple[str, ...]
    supports_vision: bool
    supports_thinking: bool
    supports_tools: bool
    context_window: int
    max_input_tokens: int
    max_output_tokens: int
    dimensions: int
    activity: str
    busy_level: str
    updated_at: str
    declared: bool = False
    bound: bool = False
    status: str = ""
    icon_file_id: int = 0
    supports_audio: bool = False
    supports_image_input: bool = False
    supports_multimodal: bool = False


@dataclass(frozen=True)
class RuntimeSetting:
    type: str = ""
    required: bool = False
    value: str = ""


@dataclass(frozen=True)
class RuntimeConfig:
    schema_version: int
    revision: int
    updated_at: str
    application: Mapping[str, Any]
    settings: Mapping[str, RuntimeSetting]
    models: Mapping[str, RuntimeModel]
    storage: Mapping[str, Any]
    tenant: Mapping[str, Any]
    features: Mapping[str, Any]


def context_from_headers(headers: Mapping[str, str]) -> RequestContext:
    """只从大小写不敏感的 HTTP headers 读取可信上下文。"""

    normalized = {key.lower(): value for key, value in headers.items()}
    return RequestContext(
        tenant_id=normalized.get("x-platform-tenant-id", ""),
        user_id=normalized.get("x-platform-user-id", ""),
        role=normalized.get("x-platform-role", ""),
        source_app_id=normalized.get("x-platform-source-app-id", ""),
        actor_type=normalized.get("x-platform-actor-type", ""),
    )


def context_from_request(request: Any) -> RequestContext:
    """从具有 headers 属性的框架请求读取可信上下文。"""

    return context_from_headers(request.headers)


def with_platform_identity(tenant_id: int, user_id: int) -> ModelAttribution:
    """把模型用量归属于平台租户中的平台用户。"""

    if type(tenant_id) is not int or type(user_id) is not int or tenant_id <= 0 or user_id <= 0:
        raise MetisError("INVALID_CONFIG", "platform tenant and user ids must be positive integers")
    return ModelAttribution("platform", tenant_id=tenant_id, user_id=user_id)


def platform_identity_from_headers(headers: Mapping[str, str]) -> ModelAttribution:
    """从平台保护入口覆盖写入的可信头构造归因。"""

    context = context_from_headers(headers)
    try:
        tenant_id = int(context.tenant_id.strip())
        user_id = int(context.user_id.strip())
    except (AttributeError, ValueError) as error:
        raise MetisError("INVALID_CONFIG", "headers do not contain a valid platform identity") from error
    return with_platform_identity(tenant_id, user_id)


def platform_identity_from_request(request: Any) -> ModelAttribution:
    """从具有 headers 属性的平台请求构造归因。"""

    if request is None or not hasattr(request, "headers"):
        raise MetisError("INVALID_CONFIG", "platform request is required")
    return platform_identity_from_headers(request.headers)


def with_external_user(external_user_id: str) -> ModelAttribution:
    """把模型用量归属于应用已自行认证的外部用户。"""

    external_user_id = external_user_id.strip()
    if (
        not external_user_id
        or len(external_user_id.encode("utf-8")) > 128
        or any(unicodedata.category(character).startswith("C") for character in external_user_id)
    ):
        raise MetisError("INVALID_CONFIG", "external user id must be 1-128 bytes without control characters")
    return ModelAttribution("external", external_user_id=external_user_id)


def without_user_attribution() -> ModelAttribution:
    """明确把无法对应到个人的后台调用归入其他用户。"""

    return ModelAttribution("other")


def _apply_attribution(headers: dict[str, str], attribution: ModelAttribution) -> None:
    for name in ("X-Platform-Tenant-Id", "X-Platform-User-Id", "X-Platform-External-User-Id"):
        headers.pop(name, None)
    if attribution.kind == "platform":
        valid = with_platform_identity(attribution.tenant_id, attribution.user_id)
        headers["X-Platform-Tenant-Id"] = str(valid.tenant_id)
        headers["X-Platform-User-Id"] = str(valid.user_id)
    elif attribution.kind == "external":
        valid = with_external_user(attribution.external_user_id)
        headers["X-Platform-External-User-Id"] = valid.external_user_id
    elif attribution.kind != "other":
        raise MetisError("INVALID_CONFIG", "model attribution is required")


class Client:
    """调用 Runtime API，并对成功发现结果缓存 30 秒。"""

    def __init__(
        self,
        platform_endpoint: str | None = None,
        app_id: str | None = None,
        app_token: str | None = None,
        service_name: str | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        cache_ttl: float = 30.0,
        opener: Any = None,
    ) -> None:
        env = dict(os.environ if environment is None else environment)
        self.platform_endpoint = (platform_endpoint or env.get("METIS_PLATFORM_ENDPOINT", "")).rstrip("/")
        self.app_id = app_id or env.get("METIS_APP_ID", "")
        self.app_token = app_token or env.get("METIS_APP_TOKEN", "")
        self.service_name = service_name or env.get("METIS_SERVICE_NAME", "")
        parsed = urllib.parse.urlparse(self.platform_endpoint)
        if not self.platform_endpoint or not self.app_id or not self.app_token:
            raise MetisError("MISSING_CONFIG", "METIS_PLATFORM_ENDPOINT, METIS_APP_ID and METIS_APP_TOKEN are required")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise MetisError("INVALID_CONFIG", "METIS_PLATFORM_ENDPOINT must be an absolute HTTP URL")
        if cache_ttl < 0:
            raise MetisError("INVALID_CONFIG", "cache_ttl cannot be negative")
        self.environment = env
        self.cache_ttl = cache_ttl
        self.opener = opener or urllib.request.urlopen
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def list_dependencies(self, refresh: bool = False) -> list[dict[str, Any]]:
        """返回 manifest 声明顺序中的全部直接依赖。"""

        values = self._get(self._path("dependencies"), refresh).get("dependencies", [])
        return [_dependency_fields(value) for value in values]

    def dependency(self, selector: str, refresh: bool = False) -> dict[str, Any]:
        """通过 alias 或应用 ID 返回一个直接依赖。"""

        return _dependency_fields(self._get(self._path("dependencies", selector), refresh))

    def web_url(self, selector: str, path: str = "") -> str:
        """返回 Web 依赖的绝对同源入口地址。"""

        if "://" in path or path.startswith("//"):
            raise MetisError("INVALID_CONFIG", "dependency path must be relative")
        parsed = urllib.parse.urlsplit(path)
        decoded_path = urllib.parse.unquote(parsed.path)
        if "\\" in decoded_path or any(part in (".", "..") for part in decoded_path.split("/")):
            raise MetisError("INVALID_CONFIG", "dependency path must stay within the application root")
        dependency = self.dependency(selector)
        if not dependency.get("available") or not dependency.get("web_base_path"):
            raise MetisError("DEPENDENCY_UNAVAILABLE", "web dependency is unavailable", 503)
        base = dependency["web_base_path"].rstrip("/") + "/" + parsed.path.lstrip("/")
        return urllib.parse.urljoin(self.platform_endpoint + "/", base.lstrip("/")) + (("?" + parsed.query) if parsed.query else "")

    def new_web_request(self, selector: str, method: str, path: str = "", data: bytes | None = None) -> urllib.request.Request:
        """创建携带应用 token 的请求，不自动复制代表用户头。"""

        return urllib.request.Request(self.web_url(selector, path), data=data, method=method, headers={"Authorization": "Bearer " + self.app_token})

    def service_endpoint(self, selector: str, endpoint_name: str, refresh: bool = False) -> dict[str, Any]:
        """返回 Service 依赖的具名 Master 代理地址。"""

        return _endpoint_fields(self._get(self._path("dependencies", selector, "endpoints", endpoint_name), refresh))

    def runtime_config(self, refresh: bool = False) -> RuntimeConfig:
        """读取当前 service 的统一运行配置快照。"""

        if not self.service_name.strip():
            raise MetisError("MISSING_CONFIG", "METIS_SERVICE_NAME is required")
        value = self._get("/api/runtime/v1/config", refresh, {"X-Metis-Service": self.service_name})
        config = value.get("config", {})
        settings = {key: RuntimeSetting(type=str(item.get("type", "")), required=bool(item.get("required", False)), value=str(item.get("value", ""))) for key, item in config.get("settings", {}).items()}
        models = {key: _runtime_model(item) for key, item in config.get("models", {}).items()}
        return RuntimeConfig(schema_version=int(value.get("schemaVersion", 0) or 0), revision=int(value.get("revision", 0) or 0), updated_at=str(value.get("updatedAt", "")), application=value.get("application", {}), settings=settings, models=models, storage=config.get("storage", {}), tenant=config.get("tenant", {}), features=config.get("features", {}))

    def _parse_model_slot(self, slot: str) -> ModelConfig | None:
        parts = slot.split(".")
        if len(parts) != 2 or parts[0] not in ("llm", "embedding", "rerank") or re.fullmatch(r"[0-9]+", parts[1]) is None:
            return None
        prefix = f"METIS_{parts[0].upper()}_{parts[1]}_"
        endpoint = self.environment.get(prefix + "ENDPOINT", "")
        model_name = self.environment.get(prefix + "MODEL", "")
        api_key = self.environment.get(prefix + "API_KEY", "")
        if not endpoint or not model_name or not api_key:
            return None

        def to_int(suffix: str) -> int:
            try:
                return int(self.environment.get(prefix + suffix, "0"))
            except ValueError:
                return 0

        def to_bool(suffix: str) -> bool:
            return self.environment.get(prefix + suffix, "").strip().lower() == "true"

        return ModelConfig(
            endpoint=endpoint,
            model=model_name,
            api_key=api_key,
            supports_vision=to_bool("SUPPORTS_VISION"),
            supports_thinking=to_bool("SUPPORTS_THINKING"),
            supports_tools=to_bool("SUPPORTS_TOOLS"),
            context_window=to_int("CONTEXT_WINDOW"),
            max_input_tokens=to_int("MAX_INPUT_TOKENS"),
            max_output_tokens=to_int("MAX_OUTPUT_TOKENS"),
            dimensions=to_int("DIMENSIONS"),
            normalized=to_bool("NORMALIZED"),
        )

    def model(self, slot: str) -> ModelConfig:
        """解析模型 slot 配置，不创建厂商客户端。未绑定或格式错误时抛出 MetisError。"""

        parts = slot.split(".")
        if len(parts) != 2 or parts[0] not in ("llm", "embedding", "rerank") or re.fullmatch(r"[0-9]+", parts[1]) is None:
            raise MetisError("INVALID_CONFIG", f"invalid model slot {slot}")
        config = self._parse_model_slot(slot)
        if config is None:
            raise MetisError("MISSING_CONFIG", f"model slot {slot} is incomplete")
        return config

    def try_model(self, slot: str) -> ModelConfig | None:
        """安全尝试读取模型 slot 配置；若未绑定或格式非法则返回 None，不抛出异常。"""

        return self._parse_model_slot(slot)

    def models(self, model_type: str) -> list[ModelConfig]:
        """批量枚举指定模型类型当前绑定的所有模型配置，按插槽序号升序排列。"""

        if model_type not in ("llm", "embedding", "rerank"):
            raise MetisError("INVALID_CONFIG", f"invalid model type {model_type}")
        result: list[ModelConfig] = []
        for i in range(10):
            config = self._parse_model_slot(f"{model_type}.{i}")
            if config is not None:
                result.append(config)
        return result

    def object_storage(self) -> dict[str, Any]:
        """解析应用 S3 配置，不连接存储或刷新凭据。"""

        get = lambda name: self.environment.get(name, "")
        result = {"endpoint": get("METIS_S3_ENDPOINT"), "region": get("METIS_S3_REGION"), "access_key": get("METIS_S3_ACCESS_KEY"), "secret_key": get("METIS_S3_SECRET_KEY"), "bucket": get("METIS_S3_BUCKET")}
        if not result["endpoint"] or not result["access_key"] or not result["secret_key"] or not result["bucket"]:
            raise MetisError("MISSING_CONFIG", "object storage config is incomplete")
        try:
            shared = json.loads(get("METIS_S3_SHARED_BUCKETS") or "[]")
        except json.JSONDecodeError as error:
            raise MetisError("INVALID_CONFIG", "METIS_S3_SHARED_BUCKETS must be a JSON string array") from error
        if not isinstance(shared, list) or not all(isinstance(item, str) for item in shared):
            raise MetisError("INVALID_CONFIG", "METIS_S3_SHARED_BUCKETS must be a JSON string array")
        result["shared_buckets"] = shared
        return result

    def application(self) -> dict[str, str]:
        """返回当前安装的非敏感身份事实。"""

        return {
            "id": self.app_id,
            "name": self.environment.get("METIS_APP_NAME", ""),
            "version": self.environment.get("METIS_APP_VERSION", ""),
            "platform_endpoint": self.platform_endpoint,
        }

    def setting(self, key: str) -> str:
        """按 manifest 原始 key 读取已注入设置。"""

        if not key.strip():
            raise MetisError("INVALID_CONFIG", "setting key is required")
        name = "METIS_SETTING_" + re.sub(r"[-. ]", "_", key.strip().upper())
        if name not in self.environment:
            raise MetisError("MISSING_CONFIG", f"setting {key!r} is not injected")
        return self.environment[name]

    def entry_port(self) -> int:
        """返回 Web 应用的 Worker 本地端口。"""

        return self._port("METIS_ENTRY_PORT")

    def endpoint_port(self, name: str) -> int:
        """返回 Service endpoint 的 Worker 本地端口。"""

        if not name.strip():
            raise MetisError("INVALID_CONFIG", "endpoint name is required")
        return self._port("METIS_ENDPOINT_" + name.strip().upper().replace("-", "_") + "_PORT")

    def _port(self, name: str) -> int:
        try:
            port = int(self.environment.get(name, ""))
        except ValueError as error:
            raise MetisError("MISSING_CONFIG", f"{name} is not a valid port") from error
        if not 1 <= port <= 65535:
            raise MetisError("MISSING_CONFIG", f"{name} is not a valid port")
        return port

    def _path(self, *parts: str) -> str:
        encoded = "/".join(urllib.parse.quote(part.strip(), safe="") for part in parts)
        return f"/api/runtime/v1/apps/{urllib.parse.quote(self.app_id, safe='')}/{encoded}"

    def _get(self, path: str, refresh: bool, extra_headers: Mapping[str, str] | None = None) -> Any:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(path)
        if not refresh and cached and cached[0] > now:
            return cached[1]
        headers = {"Authorization": "Bearer " + self.app_token}
        headers.update(extra_headers or {})
        request = urllib.request.Request(self.platform_endpoint + path, headers=headers)
        try:
            with self.opener(request) as response:
                try:
                    value = json.load(response)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise MetisError("UPSTREAM_FAILURE", "runtime response is invalid", 502) from error
        except urllib.error.HTTPError as error:
            try:
                payload = json.load(error)
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            raise MetisError(payload.get("reason", "UPSTREAM_FAILURE"), payload.get("message", "runtime request failed"), error.code) from error
        if self.cache_ttl > 0:
            with self._lock:
                self._cache[path] = (time.monotonic() + self.cache_ttl, value)
        return value


def from_env() -> Client:
    """从进程环境创建客户端。"""

    return Client()


def _dependency_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    """把 Runtime JSON 的 camelCase 字段转换成 Python SDK 的 snake_case。"""

    names = {
        "appId": "app_id",
        "requestedVersion": "requested_version",
        "resolvedVersion": "resolved_version",
        "packageSha256": "package_sha256",
        "webBasePath": "web_base_path",
        "resolutionError": "resolution_error",
    }
    return {names.get(key, key): item for key, item in value.items()}


def _endpoint_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    """把 Runtime endpoint JSON 转换成 Python SDK 的 snake_case。"""

    names = {"appId": "app_id", "endpointName": "endpoint_name"}
    return {names.get(key, key): item for key, item in value.items()}


def _runtime_model(value: Mapping[str, Any]) -> RuntimeModel:
    """把 Runtime API 的 camelCase 模型字段转换为稳定的 Python 类型。"""

    traits = value.get("traits", [])
    if not isinstance(traits, list):
        traits = []
    return RuntimeModel(
        slot=str(value.get("slot", "")), slot_type=str(value.get("slotType", "")),
        slot_index=int(value.get("slotIndex", 0) or 0), interface=str(value.get("interface", "")),
        endpoint=str(value.get("endpoint", "")), model=str(value.get("model", "")),
        display_name=str(value.get("displayName", "")), icon_url=str(value.get("iconUrl", "")),
        description=str(value.get("description", "")), traits=tuple(str(item) for item in traits),
        supports_vision=bool(value.get("supportsVision", False)), supports_thinking=bool(value.get("supportsThinking", False)),
        supports_tools=bool(value.get("supportsTools", False)), context_window=int(value.get("contextWindow", 0) or 0),
        max_input_tokens=int(value.get("maxInputTokens", 0) or 0), max_output_tokens=int(value.get("maxOutputTokens", 0) or 0),
        dimensions=int(value.get("dimensions", 0) or 0), activity=str(value.get("activity", "unknown")),
        busy_level=str(value.get("busyLevel", "unknown")), updated_at=str(value.get("updatedAt", "")),
        declared=bool(value.get("declared", False)), bound=bool(value.get("bound", False)), status=str(value.get("status", "")),
        icon_file_id=int(value.get("iconFileId", 0) or 0), supports_audio=bool(value.get("supportsAudio", False)),
        supports_image_input=bool(value.get("supportsImageInput", False)), supports_multimodal=bool(value.get("supportsMultimodal", False)),
    )
