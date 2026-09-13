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


class Client:
    """调用 Runtime API，并对成功发现结果缓存 30 秒。"""

    def __init__(
        self,
        platform_endpoint: str | None = None,
        app_id: str | None = None,
        app_token: str | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        cache_ttl: float = 30.0,
        opener: Any = None,
    ) -> None:
        env = dict(os.environ if environment is None else environment)
        self.platform_endpoint = (platform_endpoint or env.get("METIS_PLATFORM_ENDPOINT", "")).rstrip("/")
        self.app_id = app_id or env.get("METIS_APP_ID", "")
        self.app_token = app_token or env.get("METIS_APP_TOKEN", "")
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

    def model(self, slot: str) -> dict[str, Any]:
        """解析模型 slot 配置，不创建厂商客户端。"""

        parts = slot.split(".")
        if len(parts) != 2 or parts[0] not in ("llm", "embedding", "rerank") or re.fullmatch(r"[0-9]+", parts[1]) is None:
            raise MetisError("INVALID_CONFIG", f"invalid model slot {slot}")
        prefix = f"METIS_{parts[0].upper()}_{parts[1]}_"
        values = {name.removeprefix(prefix): value for name, value in self.environment.items() if name.startswith(prefix)}
        if not values.get("ENDPOINT") or not values.get("MODEL") or not values.get("API_KEY"):
            raise MetisError("MISSING_CONFIG", f"model slot {slot} is incomplete")
        return {"endpoint": values["ENDPOINT"], "model": values["MODEL"], "api_key": values["API_KEY"], "values": values}

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

    def _get(self, path: str, refresh: bool) -> Any:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(path)
        if not refresh and cached and cached[0] > now:
            return cached[1]
        request = urllib.request.Request(self.platform_endpoint + path, headers={"Authorization": "Bearer " + self.app_token})
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
