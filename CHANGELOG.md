# Changelog

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
