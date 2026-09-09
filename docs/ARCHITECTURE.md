# 架构边界

本项目采用“本地优先、适配器可替换”的单进程架构。FastAPI 负责页面和 API，媒体处理仍在本机工作目录完成；模型服务通过明确的适配器接入。

## 当前模块

| 模块 | 责任 | 不应放入 |
| --- | --- | --- |
| `app/main.py` | 路由、请求校验、工作流编排 | 新的模型目录、持久化格式、通用 UI 数据 |
| `app/domain.py` | `Job`、`BrollClip` 等工作流数据模型 | HTTP、文件读写、模型调用 |
| `app/job_store.py` | 任务 JSON 持久化、旧任务迁移、重启中断标记 | FastAPI 路由和媒体处理 |
| `app/storage.py` | 通用 JSON 原子读写 | 业务字段和模型逻辑 |
| `app/model_registry.py` | 模型能力目录、路由选项、纯转换函数 | API Key、网络请求、任务状态 |
| `app/service_connections.py` | 供应商地址校验、模型发现、连接与路由 JSON 持久化 | 工作流执行和页面 DOM |
| `app/digital_human.py` | MuseTalk / HeyGem 等数字人提供商注册与调用 | 页面状态和任务 JSON |
| `app/local_engine.py`、`app/local_runtime.py` | 本地引擎安装、启动和状态探测 | 文案、字幕、封面业务规则 |
| `app/static/template-catalog.js` | 标题、字幕、封面模板静态目录 | DOM 操作和 API 请求 |
| `app/static/app.js` | 页面状态、事件和 API 调用 | 大段模板数据和后端业务规则 |

## 扩展约定

1. 新增模型时先更新 `model_registry.py`；只有需要特殊协议时才新增适配器，不在路由函数中复制一套 `if/elif`。
2. 新增数字人时实现 `DigitalHumanProvider`，在 `DIGITAL_HUMAN_REGISTRY` 注册，并为缺少配置提供可读错误。
3. 新增持久化数据必须使用 `storage.write_json_atomic`，不要直接覆盖 JSON 文件。
4. 前端新增模板只改 `template-catalog.js`；控制器只消费目录，不重新定义模板数据。
5. 媒体任务必须能在没有 GPU 和真实模型时用 Mock/本地静音流程跑通 API 状态机。

## 下一阶段拆分顺序

后续若继续降低 `main.py` 的体积，按以下顺序进行：

1. 把 FFmpeg、音频、视频规范化整理为 `app/media_tools.py`；
2. 把文案、配音、改口型、成片处理整理为 `app/workflows/`；
3. 最后把 FastAPI 路由拆成 `app/api/`，并保持旧端点兼容。

每次只移动一个边界，先跑 Python 回归测试和 Playwright 冒烟测试，再继续下一步。
