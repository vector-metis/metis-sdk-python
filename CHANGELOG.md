# Changelog

## 0.4.2

- Runtime Config 的 protobuf `int64/uint64` 字段统一以字符串暴露，避免隐式整数转换和精度丢失。

## 0.4.0

- 用 `Client.runtime_config()` 统一读取当前 service 的 settings、models、storage、tenant、features 和 revision。
- 删除已废弃的 `list_runtime_models()` 及旧 `/api/runtime/v1/apps/{app_id}/models` 路由。
- 增加 `METIS_SERVICE_NAME` 与完整 RuntimeModel 展示和能力字段。

## 0.3.1

- 增加 `Client.list_runtime_models()`，读取当前应用模型 slot 的展示元数据和非敏感能力字段。

## 0.3.0

- 增加平台用户、外部用户和其他用户三类模型用量归因。
- 增加始终携带 slot API key 和显式归因的 `ModelConfig.new_request()`。

## 0.2.0

- 强类型重构 `ModelConfig`，彻底移除弱类型 `values` 字典。
- 增加模型特性与卡片参数属性：`supports_vision`, `supports_thinking`, `supports_tools`, `context_window`, `max_input_tokens`, `max_output_tokens`, `dimensions`, `normalized`。
- 增加批量模型枚举 `models(model_type)` 与安全探活 `try_model(slot)` API。

## 0.1.2

- 首次公开发布。
## 0.1.2

- Normalize Runtime dependency and endpoint fields to Python snake_case and expose version lock metadata.
