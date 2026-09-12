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
```

运行环境必须提供 `METIS_PLATFORM_ENDPOINT`、`METIS_APP_ID` 和 `METIS_APP_TOKEN`。本地测试可以向 `Client` 显式传入配置。SDK 不创建厂商模型/存储客户端，也不隐藏重试。

## 开发

```bash
python -m pytest
python -m build
```

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
