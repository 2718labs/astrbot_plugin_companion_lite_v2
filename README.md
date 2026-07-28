<div align="center">
  <img src="logo.png" width="88" alt="CompanionLite V2 Logo" />
  <h1>CompanionLite V2</h1>
  <p><strong>让关系成为可观察、可裁决、可修复的对话上下文。</strong></p>
  <p>面向 AstrBot 私聊场景的轻量关系侧车。它观察互动方式、维护关系状态，并把本轮投入、距离、处境、感受与表达编译成主人格可以直接执行的自然语言。</p>
  <p>
    <a href="https://github.com/AstrBotDevs/AstrBot"><img src="https://img.shields.io/badge/AstrBot-Plugin-5B67F1?style=flat-square" alt="AstrBot Plugin" /></a>
    <a href="https://github.com/6TBWhite/astrbot_plugin_companion_lite_v2/releases/tag/v2.0.1"><img src="https://img.shields.io/badge/release-v2.0.1-7357D9?style=flat-square" alt="Release v2.0.1" /></a>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3" /></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2F855A?style=flat-square" alt="MIT License" /></a>
  </p>
  <p>
    <a href="#核心能力">核心能力</a> ·
    <a href="#架构一览">架构一览</a> ·
    <a href="#安装与启用">安装使用</a> ·
    <a href="docs/semantic-design.md">语义设计</a> ·
    <a href="CHANGELOG.md">版本变化</a>
  </p>
</div>

## 产品定位

CompanionLite V2 不替代主人格，也不保存用户事实。LivingMemory 一类长期记忆组件可以回答“这个用户是谁、发生过什么”，CompanionLite V2 只负责“这轮愿意投入多少、保持什么距离、是否需要立界限”。

关系分析模型只提交证据，不直接决定姿态、权限或绝对数值。关系升级、修复、三维变化和严重事件例外均由代码统一裁决；主人格最终看到的是简短自然语言，而不是内部评分、事件编号或分析标签。

当前稳定版本为 `2.0.1`。新安装默认使用 `observe`，只记录、分析并生成预览；确认行为符合预期后再切换为 `active`，让关系上下文进入主模型请求。

## 为什么另做 V2

V1 在多轮重构后已不再适合作为运行基线，因此本项目以独立插件 ID、配置、数据库、命令和 Web 路由重新建立精简模型。

- 小模型观察证据，代码掌握关系状态的最终解释权。
- 三维关系、当前姿态和一个主导问题共同表达长期关系与眼前处境。
- 严重事件在主回复前按需复核，普通消息不增加额外模型调用。
- 主模型只接收五标签自然语言上下文，不读取内部裸状态。
- 插件只写入自己的数据目录，不迁移或修改其他关系、记忆插件的数据。

## 核心能力

| 工作域    | 能力                                           |
| ------ | -------------------------------------------- |
| 独立关系档案 | 按完整 UMO 隔离熟悉度、信任、亲和、姿态、当前问题、消息缓冲和分析队列        |
| 分层关系分析 | 每两个完整来回轻分析一次，每六轮深分析一次；轻、深结果共用同一套代码裁决         |
| 边界与修复  | 单向索取逐步收门；贬低、强迫和边界施压可提前复核；修复必须由两次独立行为确认       |
| 正式关系   | 管理员按人格明确绑定唯一窗口；身份允许自然亲近，但不覆盖未解决问题与当前边界       |
| 语义编译   | 将状态压缩为 `投入 / 关系 / 处境 / 感受 / 表达` 五个自然语言字段     |
| 可观测管理  | 关系档案展示侧写、六轮节奏、语义投影、消息时间线和工程诊断；启停管理总览全部已知 UMO |

## 架构一览

![CompanionLite V2 对话管线](docs/assets/pipeline.svg)

图中只保留稳定主线：普通消息在主回复前不会调用分析模型，轻分析与深分析都在主人格回复完成后运行。完整轮次门控、分值阈值、修复与绑定优先级见[语义与状态设计](docs/semantic-design.md)。

## 安装与启用

### 安装

推荐在 AstrBot WebUI 的插件市场中搜索“陪伴Lite V2”并安装。若暂未检索到，可从 [GitHub Releases](https://github.com/6TBWhite/astrbot_plugin_companion_lite_v2/releases) 下载 ZIP，然后在 AstrBot WebUI 中进入“插件 → 安装插件 → 从文件安装”并上传。

也可以在支持仓库地址安装的界面中使用：

```text
https://github.com/6TBWhite/astrbot_plugin_companion_lite_v2
```

运行要求：

- AstrBot `>=4.26.3`
- 可用的默认模型提供商，或单独配置关系分析 Provider
- 私聊会话；群聊不进入关系处理

安装或更新后，请在“插件 → AstrBot 插件”中重载 CompanionLite V2，或重启 AstrBot。

### 首次启用

1. 保持默认 `operation_mode = observe`。
2. 完成几轮私聊，在调试页检查关系侧写、分析证据和下一轮语义预览。
3. 确认其他插件没有向同一轮注入冲突的关系指令。
4. 将 `operation_mode` 切换为 `active` 并重载插件。

`observe` 仍会捕获消息、运行关系分析并保存预览，但不会修改主人格请求。`active` 会加入稳定的 `companion_protocol` 和本轮动态 `companion_state`。

插件数据默认位于：

```text
data/plugin_data/astrbot_plugin_companion_lite_v2/
└── companion_lite_v2.db
```

## 配置

以下配置均可在 AstrBot WebUI 修改，并与 `_conf_schema.json` 保持一致。

| 配置项                      | 默认值       | 说明                                    |
| ------------------------ | ---------:| ------------------------------------- |
| `operation_mode`         | `observe` | `observe` 只分析和预览；`active` 向主人格注入关系上下文 |
| `enable_message_capture` | `true`    | 捕获私聊完整来回                              |
| `min_message_length`     | `1`       | 进入关系消息缓冲的最短字符数                        |
| `max_message_length`     | `400`     | 单条用户消息和主人格回复的入库上限                     |
| `max_buffer_rounds`      | `24`      | 每个 UMO 保留的完整来回数                       |
| `reflection_provider_id` | `""`      | 留空时使用当前默认模型提供商                        |
| `persona_prompt`         | `""`      | 深分析使用的稳定人格参考                          |
| `max_context_chars`      | `340`     | 动态关系上下文预算，可配置范围 `260..340`            |

## 管理员命令

命令只在私聊中使用。

| 命令                  | 功能                           |
| ------------------- | ---------------------------- |
| `/clv2_status`      | 查看当前 UMO 的关系状态和最近编译文本        |
| `/clv2_reset`       | 清空当前 UMO 的关系状态、消息、待处理交互和正式绑定 |
| `/clv2_reflect`     | 尝试运行当前轮次的深分析                 |
| `/companion_bond`   | 将当前窗口设为当前人格唯一正式关系            |
| `/companion_unbond` | 解除当前窗口的正式身份，保留相处状态           |

## 调试页

在 AstrBot WebUI 左侧“插件页面”中打开“陪伴Lite V2”，或访问：

```text
http://<AstrBot 地址>/#/plugin-page/astrbot_plugin_companion_lite_v2/debug
```

页面提供两个视图：

- **关系档案**：查看三维状态、感受追踪、关系侧写、六轮互动节奏、五标签语义投影、消息时间线和工程诊断。
- **启停管理**：总览最多 1000 个已知 UMO，按状态搜索、筛选、排序并逐会话启停。

关系档案不会重复提供启停开关。页面每五秒静默同步；同步会保护文字选择、输入焦点、滚动位置、语义页签和折叠状态。

## 项目结构

```text
astrbot_plugin_companion_lite_v2/
├── main.py                 AstrBot 事件钩子、命令、调度与 Web 路由
├── config.py               配置读取与边界归一化
├── core/
│   ├── models.py           关系状态、证据类型与代码裁决
│   └── storage.py          SQLite 持久化与 UMO 隔离
├── llm/
│   ├── reflection.py       严重、轻量与深度关系分析
│   └── context_builder.py  五标签语义编译
├── pages/debug/index.html  关系档案与启停管理页面
├── docs/                   语义与状态设计
├── tests/                  状态、提示词、存储与 WebUI 测试
├── _conf_schema.json       AstrBot WebUI 配置定义
└── metadata.yaml           插件市场元数据
```

## 设计与文档

- [语义与状态设计](docs/semantic-design.md)：关系状态、调度、证据裁决、修复、绑定与五标签编译的当前设计契约。
- [项目状态](PROJECT_STATE.md)：当前运行状态、已验证结果和后续检查项。
- [版本变化](CHANGELOG.md)：从早期预览到稳定版的完整演进记录。
- [MIT License](LICENSE)

## 开发验证

在插件目录运行：

```text
python -m pytest -q
python -m ruff check .
```

<div align="center">
  <a href="https://github.com/AstrBotDevs/AstrBot">AstrBot Plugin</a> ·
  <a href="LICENSE">MIT License</a>
</div>
