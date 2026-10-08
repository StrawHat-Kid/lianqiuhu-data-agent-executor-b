# 练秋湖 HC2026 纯问数执行器接入说明

## 用途与技术栈

本执行器用于基于练秋湖业务数据进行单轮问数。它是一个 FastAPI + Uvicorn HTTP 执行器，通过 SiliconFlow（硅基流动）OpenAI Compatible Chat Completions API 生成回答。

当前实现会在每次提问时读取 `knowedge/` 顶层的全部普通文件，将文件内容拼接到本次用户消息中。它不是向量知识库：没有 embedding、向量数据库、索引、SQL 或自动分块检索。

## 目录与配置

```text
lianqiuhu-data-agent-executor/
  knowedge/                 # HC 正式数据后续放入此目录
  doc/                      # 接入、模板与 Postman 文档
  config.example.json       # 可提交的脱敏示例
  config.json               # 仅本机/部署环境使用，已被 Git 忽略
  main.py
  deepseek_client.py
  file_store.py
  sessions.py
  start-executor.bat
```

目录名必须保持为 `knowedge`。这是当前配置和核心代码使用的名称。

`knowedge/` 顶层只能放入以下 4 个正式知识文件，执行器会按文件名排序后全文读取：

1. `00_回答规则.md`
2. `01_权威指标事实表.csv`（696 条事实）
3. `02_事件告警表.csv`（35 条事件）
4. `03_指标词典与口径.csv`（315 条指标词典）

不得保留旧版 7 文件，也不要把 ZIP、`validation/` 文件或子目录放入 `knowedge/`，否则会被一并注入模型上下文。设施管理的“工单数量”“告警数”“时长”中 01～31 表示本月第 1～31 日，属于月内日序列，不是小时序列。

首次部署时，将 `config.example.json` 复制为 `config.json`，再填写部署环境提供的 SiliconFlow API Key。不得提交真实 Key。当前模型服务商为 SiliconFlow（硅基流动），OpenAI Compatible Base URL 为 `https://api.siliconflow.cn/v1`，模型为 `deepseek-ai/DeepSeek-V4-Flash`。原 DeepSeek 官方 API 不再作为当前 HC 调用地址。默认监听为 `0.0.0.0:18034`。

`callback.token` 只用于执行器向 `context.callback` 回传结果时的 `X-Auth-Token`。它不是产品侧 INGRESS_TOKEN；当前执行器代码没有实现入站 INGRESS_TOKEN 鉴权。

## 本地启动与健康检查

Windows 下运行 `start-executor.bat`。脚本会在项目目录创建或复用 `.venv`，安装 `requirements.txt` 后执行 `main.py`。

健康检查：`GET http://127.0.0.1:18034/api/health`

Linux 可在安装依赖后，从项目根目录执行 `python main.py`。相对路径配置依赖当前工作目录。

## 接口

| 方法 | Path | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| POST | `/api/chat` | 本地直接问答 |
| POST | `/api/commands` | RUISI 指令入口 |
| POST | `/api/files/upload` | 上传本次问答附件 |
| POST | `/api/files/import` | 导入执行器机器上的本地文件，仅限受控联调 |
| GET | `/api/files` | 列出已上传附件 |
| GET | `/api/sessions` | 列出进程内 Session 条目 |
| DELETE | `/api/sessions/{sid}` | 清空指定 Session |

`POST /api/chat`：

```json
{"question":"请查询某指标","files":"可选，多个文件引用以逗号分隔"}
```

`POST /api/commands` 支持 JSON 数组或以下信封：

```json
{
  "context": {
    "agent": "产品侧智能体标识",
    "reply_to": "触发用户标识",
    "callback": "产品侧回程地址",
    "groupchat": false
  },
  "commands": [
    {
      "action": "AI智能问答",
      "params": {"问题": "请查询某指标"}
    }
  ]
}
```

可用 action：

- `AI智能问答`：必填参数 `问题`；可选参数 `文件`。
- `AI问答会话重置`：无参数。

当前为单轮问答。虽然项目保留 Session 接口，但问答主流程不会保存历史消息。

## 回程 callback

当 `context` 中同时包含 `callback` 与 `reply_to` 时，执行器在完成问答后向 callback 发起 JSON POST：

```json
{
  "agent": "产品侧传入的 agent",
  "to": "context.reply_to",
  "body": "完整回答文本",
  "groupchat": false
}
```

若 `config.json` 配置了 `callback.token`，请求 Header 为 `X-Auth-Token: <callback.token>`。产品侧 callback 地址必须能被执行器访问。

## 产品侧注册要点

- transport：`http`
- method：`POST`
- endpoint：`http://<执行器可达地址>:18034/api/commands`
- command 名称必须与 `AI智能问答`、`AI问答会话重置` 完全一致。
- 如需回程，产品侧传入上述 context 信封。
- 上游 SiliconFlow 可流式读取，但本执行器对产品侧返回的是普通 HTTP JSON，不提供对外 SSE。

## 数据规模限制

每次问答会读取整个 `knowedge/` 目录。单文件最多注入 500,000 字符，全部文件内容最多注入 1,200,000 字符。超出部分会被截断。

当 HC 数据量较大，或需要严格、可复现的聚合计算时，应在正式上线前评估结构化查询、预计算或检索方案；不能仅通过继续增加 Prompt 内容解决。
