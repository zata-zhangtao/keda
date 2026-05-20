# Prompt 工程

本仓库作为 AI Agent 模板，建议将 Prompt 相关资产作为一等工程对象维护。

## 目标

- 保持 Prompt 结构可读、可评审、可版本追踪。
- 将 Prompt 变更与评测结果关联，降低回归风险。

## 建议目录策略

可按业务域或模型能力分类，例如：

```text
prompt/
├── classification/
├── extraction/
└── planning/
```

## Prompt 维护规范

- 每个 Prompt 文件建议包含：用途、输入约束、输出格式、失败兜底。
- 对关键 Prompt 建立最小可复现实验样例。
- 对系统 Prompt 变更进行评审，记录影响范围。

## 与模型配置协同

`src/backend/infrastructure/models/model_loader.py` 提供了模型配置加载能力，可将 Prompt 路由策略与模型选择策略联动设计。

## TODO

- TODO: 增加 Prompt 版本管理约定。
- TODO: 增加多模型 Prompt A/B 评测模板。
