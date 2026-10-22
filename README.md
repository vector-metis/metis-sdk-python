# Metis Python SDK

Metis 应用后端 SDK，用于读取统一应用身份、可信请求上下文、声明的 Web/Service 依赖，以及模型和对象存储配置。

## 安装

```bash
python -m pip install metis-platform-sdk
```

```python
from metis_sdk import from_env

client = from_env()
for dependency in client.list_dependencies():
    print(dependency["app_id"])
embedding = client.model("embedding.0")
rerank = client.model("rerank.0")
print(embedding["model"], rerank["model"])
```

`model()` 支持 `llm.N`、`embedding.N` 和 `rerank.N` 三类 slot，并返回对应的网关地址、模型别名、API key 及类型专属参数。SDK 不创建厂商客户端。

`runtime_config()` 返回当前 service 的模型展示名称、图标、能力、上下文限制、状态和 revision，不返回 API key 或供应商凭据：

```python
config = client.runtime_config()
for slot, model in config.models.items():
    print(slot, model.display_name, model.busy_level)
```

Runtime Config 中原始 protobuf 类型为 `int64` 或 `uint64` 的字段（例如 `revision`、`installation_id`、`context_window` 和 `icon_file_id`）在 protojson 中使用十进制字符串，Python SDK 也保持为 `str`。SDK 不把这些值转换为整数或根据内容猜测类型；需要计算时由应用显式调用 `int(value)`。`int32/uint32` 和有限浮点字段仍按数字处理。

模型 slot API key 始终必填，请显式选择最终用户归因：

```python
from metis_sdk import platform_identity_from_request, with_external_user, without_user_attribution

request = model.new_request("POST", "/v1/chat/completions", platform_identity_from_request(incoming_request), body)
request = model.new_request("POST", "/v1/chat/completions", with_external_user("customer-42"), body)
request = model.new_request("POST", "/v1/embeddings", without_user_attribution(), body)
```

优先从平台保护入口的可信请求头构造平台用户归因。外部用户归因只适用于应用已自行认证的用户；无法对应到个人的后台任务才归入其他用户。浏览器端不得持有模型 slot API key。

运行环境必须提供 `METIS_PLATFORM_ENDPOINT`、`METIS_APP_ID` 和 `METIS_APP_TOKEN`。本地测试可以向 `Client` 显式传入配置。SDK 不创建厂商模型/存储客户端，也不隐藏重试。

依赖和 endpoint 返回字段会转换为 Python 风格：依赖使用 `app_id`、`requested_version`、
`resolved_version`、`package_sha256`、`direct`、`available`、`resolution_error`、`app_type` 和
`web_base_path`；应用应在使用可选依赖前检查 `available`。

## 开发

```bash
python -m pytest
python -m build
```

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
