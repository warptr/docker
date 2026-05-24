# AI识别增强

`AI识别增强` 用来补强 MoviePilot 原生整理链里的识别阶段。

它的核心思路很简单：

- 复用 MoviePilot 当前已经启用的 LLM 配置
- 在原生识别失败或置信度不足时，做一次本地结构化识别兜底
- 把结果回写给 MoviePilot，继续走原生二次识别和后续整理链

## 适合什么场景

- 文件名比较脏，混有压制组、分辨率、语言、站点标记
- 同一部剧经常出现英文名、别名、原名、翻译名混用
- 网盘挂载、手动整理、历史资源补录时，原生识别偶尔不稳定
- 你想把失败样本沉淀下来，后面持续优化 `CustomIdentifiers`

## 和 MoviePilot 原版智能体的区别

MoviePilot 原版智能体已经提供“整理失败后自动接管再试一次”的能力。

这和 `AI识别增强` 有重叠，但定位不同：

- **MP 原版智能体**
  - 更偏“一次性补救”
  - 适合偶发失败、想省事的场景

- **AI识别增强**
  - 更偏“识别失败治理层”
  - 除了补救当前这次，还能：
    - 保存失败样本
    - 汇总样本洞察
    - 生成 `CustomIdentifiers` 建议
    - 写入识别词
    - 重放 / 复查 / 批量出队

一句话区分：

- 原版智能体：自动接管一次
- `AI识别增强`：把失败样本沉淀下来，长期减少同类失败

## 当前能力

- 监听 `ChainEventType.NameRecognize`
- 用当前 LLM 结构化判断标题、年份、类型、季集
- 回写 `name / year / season / episode`
- 交回 MoviePilot 原生链路继续二次识别
- 保存低置信度失败样本
- 提供失败样本工作清单、洞察、重放、删除和清空能力
- 生成并应用 `CustomIdentifiers` 建议

## 主要接口

- `GET /api/v1/plugin/AIRecognizerEnhancer/health`
  - 查看插件状态、LLM 提供方、模型、阈值和超时配置
- `POST /api/v1/plugin/AIRecognizerEnhancer/recognize`
  - 对单个标题做一次本地结构化识别测试
- `GET /api/v1/plugin/AIRecognizerEnhancer/failed_samples`
  - 查看最近保存的失败样本
- `GET /api/v1/plugin/AIRecognizerEnhancer/sample_worklist`
  - 返回适合继续处理的失败样本摘要列表
- `GET /api/v1/plugin/AIRecognizerEnhancer/sample_insights`
  - 汇总失败原因、重复问题和优先处理样本
- `POST /api/v1/plugin/AIRecognizerEnhancer/replay_failed_sample`
  - 用当前识别词和当前识别器重放复查某条失败样本
- `POST /api/v1/plugin/AIRecognizerEnhancer/suggest_identifiers_from_sample`
  - 直接基于失败样本生成识别词建议
- `POST /api/v1/plugin/AIRecognizerEnhancer/apply_suggested_identifier`
  - 把建议规则写入系统 `CustomIdentifiers`

其余批量接口和清理接口可以按需要继续使用，详细路径以插件 `get_api()` 暴露结果为准。

## 配置建议

- 先确认 MoviePilot 本身已经配置好可用的 LLM
- 建议保持“保存失败样本”开启
- 如果你经常处理历史资源或网盘资源，建议定期查看：
  - `failed_samples`
  - `sample_worklist`
  - `sample_insights`

## 已验证情况

当前版本：`0.1.12`

当前 Release：https://github.com/liuyuexi1987/MoviePilot-Plugins/releases/tag/v0.2.68

这版已经验证过：

- 最新版 MoviePilot 下可以正常加载
- 正常中文标题识别可用
- 英文别名、韩文原名、中文别名可识别回标准媒体信息
- 低置信度标题会落失败样本
- `replay_failed_sample` 复查链可用

## 说明

- 这个插件不依赖外部 AI Gateway 回调链
- 重点是增强识别，不负责替代 MoviePilot 全部整理流程
- 如果你只是偶发整理失败，原版智能体可能已经够用
- 如果你长期受命名混乱困扰，这个插件更有价值
