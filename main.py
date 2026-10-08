"""AI问答客户端执行器（HTTP 类型睿司执行器）。

接收 RUISI-OSCA 分发过来的指令（HTTP JSON 数组或带 context 的信封），
与 DeepSeek 官方 API 进行聊天交互，支持上传数据文件作为上下文，
并把 AI 输出通过回程通道（callback）返回给睿思。

常用端口：8009
"""
import json
import logging
import os
import sys
import threading
import traceback
from typing import List, Optional

import requests
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from deepseek_client import DeepSeekClient, DeepSeekError
from file_store import FileStore
from sessions import SessionStore

# ---------------------------------------------------------------------------
# 日志（显式 UTF-8，避免中文日志乱码）
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _init_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)
    try:
        os.makedirs("logs", exist_ok=True)
        fh = logging.FileHandler("logs/executor.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(fh)
    except Exception as e:
        print(f"日志文件初始化失败: {e}")


_init_logging()
log = logging.getLogger("main")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("EXECUTOR_CONFIG", "config.json")
DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8009},
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-v4-flash",
        "thinking": "enabled",
        "reasoning_effort": "medium",
        "max_tokens": 8192,
        "stream": True,
        "timeout_seconds": 300,
        "max_retries": 2,
        "retry_delay_seconds": 2,
        "system_prompt": "你是一个严谨的数据智能分析助手。用户每次提问时，都会在问题末尾附上知识库全部文件内容。请务必以知识库内容为依据作答，认真阅读并精确计算，不要编造数据；如果知识库中没有相关信息，请如实说明。回答时直接给出结论和计算过程摘要，使用中文作答。",
    },
    "knowledge_base": {"dir": "knowedge", "max_file_chars": 500000, "max_total_chars": 1200000},
    "upload": {"dir": "data/uploads", "max_file_size_mb": 64, "max_file_chars": 500000},
    "sessions": {"max_history": 20},
    "callback": {"token": ""},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg = _deep_merge(cfg, user_cfg)
    else:
        log.warning("未找到配置文件 %s，使用内置默认配置。", CONFIG_PATH)
    return cfg


def _deep_merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


CONFIG = load_config()

DEEPSEEK_CFG = CONFIG["deepseek"]
KB_CFG = CONFIG["knowledge_base"]
UPLOAD_CFG = CONFIG["upload"]
SESSION_CFG = CONFIG["sessions"]
SERVER_CFG = CONFIG["server"]
CALLBACK_TOKEN = (CONFIG.get("callback") or {}).get("token", "")

client = DeepSeekClient(DEEPSEEK_CFG)
file_store = FileStore(UPLOAD_CFG)
sessions = SessionStore(SESSION_CFG)

# 指令名（与《指令设计总表》保持一致）
CMD_AI_QA = "AI智能问答"
CMD_AI_RESET = "AI问答会话重置"

app = FastAPI(title="AI问答客户端执行器", version="1.0.0")


# ---------------------------------------------------------------------------
# 指令处理
# ---------------------------------------------------------------------------
def _session_id(context: Optional[dict]) -> str:
    """会话由执行器内部自动管理，无需外部传参。

    有上下文信封时按触发者 reply_to 区分用户；否则统一使用 default 会话。
    """
    if context and context.get("reply_to"):
        return "user:" + str(context["reply_to"])
    return "default"


def _split_refs(raw):
    if raw is None:
        return []
    text = str(raw).replace("，", ",").replace(";", ",").replace("；", ",")
    return [x.strip() for x in text.split(",") if x.strip()]


def _resolve_file_list(params: dict):
    refs = []
    for key in ("文件", "files", "附件", "attachments"):
        if params.get(key):
            refs.extend(_split_refs(params[key]))
    if not refs:
        return []
    paths = []
    skipped = []
    for ref in refs:
        p = file_store.resolve_path(ref)
        if p:
            paths.append(p)
        else:
            skipped.append(ref)
    if skipped:
        log.warning("未能解析的文件引用: %s", skipped)
    return paths


def load_knowledge_base() -> str:
    """读取知识库目录下全部文本文件，拼成一段知识库内容。

    单轮会话模式：每次提问都会把知识库内容拼接到问题尾部一起发给大模型。
    知识库路径为相对路径（相对程序主目录，默认 knowedge），支持自动识别编码。
    """
    kb_dir = KB_CFG.get("dir", "knowedge")
    max_file_chars = int(KB_CFG.get("max_file_chars", 500000))
    max_total_chars = int(KB_CFG.get("max_total_chars", 1200000))
    if not os.path.isdir(kb_dir):
        log.warning("知识库目录不存在: %s", kb_dir)
        return ""

    files = [os.path.join(kb_dir, name) for name in sorted(os.listdir(kb_dir))
             if os.path.isfile(os.path.join(kb_dir, name))]
    if not files:
        log.warning("知识库目录为空: %s", kb_dir)
        return ""

    blocks = []
    total = 0
    capped = False
    for i, p in enumerate(files, 1):
        fname = os.path.basename(p)
        try:
            content = file_store.read_text(p, max_chars=max_file_chars)
        except Exception as e:
            content = f"（读取失败：{e}）"
        remaining = max_total_chars - total
        if remaining <= 0:
            capped = True
            break
        if len(content) > remaining:
            content = content[:remaining] + "\n...[知识库总长受限，已截断]"
            capped = True
        blocks.append(f"===== 知识库文件 {i}: {fname} =====\n{content}")
        total += len(content)
        if capped:
            break
    log.info("知识库加载: %d 个文件, 约 %d 字符%s", len(files), total, "（已按上限截断）" if capped else "")
    body = "\n\n".join(blocks)
    if capped:
        body += "\n\n（知识库内容过长，已按上限截断）"
    return body


def _build_user_content(question: str, file_paths: list, kb_text: str) -> str:
    blocks = [question]
    if file_paths:
        blocks.append("以下是用户本次额外指定的文件内容：")
        for i, p in enumerate(file_paths, 1):
            fname = os.path.basename(p)
            try:
                content = file_store.read_text(p)
            except Exception as e:
                content = f"（读取失败：{e}）"
            blocks.append(f"===== 文件 {i}: {fname} =====\n{content}")
    if kb_text:
        blocks.append("以下是知识库全部文件内容，请严格基于这些内容作答：\n\n" + kb_text)
    return "\n\n".join(blocks)


def handle_ai_qa(params: dict, context: Optional[dict]) -> dict:
    question = params.get("问题") or params.get("question") or ""
    question = question.strip()
    if not question:
        return {"ok": False, "error": "缺少必填参数：问题"}
    sid = _session_id(context)
    file_paths = _resolve_file_list(params)
    kb_text = load_knowledge_base()

    # 单轮会话：每次提问都是独立请求，只发送 系统提示词 + 本次问题（尾部拼接知识库）
    messages = [
        {"role": "system", "content": DEEPSEEK_CFG.get("system_prompt", "")},
        {"role": "user", "content": _build_user_content(question, file_paths, kb_text)},
    ]

    log.info("[%s] 提问: %s | 知识库字符数: %d | 额外文件数: %d",
             sid, question, len(kb_text), len(file_paths))
    try:
        answer = client.chat(messages)
    except Exception as e:
        log.error("[%s] DeepSeek 调用失败: %s", sid, e)
        return {"ok": False, "error": str(e), "question": question}

    log.info("[%s] 回答长度: %d 字符", sid, len(answer))
    return {
        "ok": True,
        "question": question,
        "answer": answer,
        "session_id": sid,
        "files_used": len(file_paths),
        "knowledge_base_chars": len(kb_text),
    }


def handle_ai_reset(params: dict, context: Optional[dict]) -> dict:
    sid = _session_id(context)
    sessions.clear(sid)
    return {"ok": True, "message": "会话已重置", "session_id": sid}


HANDLERS = {
    CMD_AI_QA: handle_ai_qa,
    CMD_AI_RESET: handle_ai_reset,
}


def process_commands(commands: list, context: Optional[dict]) -> list:
    results = []
    for cmd in commands or []:
        action = cmd.get("action", "")
        params = cmd.get("params", {}) or {}
        handler = HANDLERS.get(action)
        if not handler:
            log.warning("未知指令: %s", action)
            results.append({"action": action, "ok": False, "error": "未知指令"})
            continue
        try:
            result = handler(params, context)
        except Exception as e:
            log.error("处理指令 %s 异常: %s\n%s", action, e, traceback.format_exc())
            result = {"ok": False, "error": str(e)}
        result.setdefault("action", action)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# 回程通道（把 AI 输出返回给睿思）
# ---------------------------------------------------------------------------
def send_callback(context: Optional[dict], body: str):
    if not context or not body:
        return
    callback = context.get("callback")
    to = context.get("reply_to")
    if not callback or not to:
        log.warning("回程所需字段缺失（callback/reply_to），跳过回程。context=%s", context)
        return
    payload = {
        "agent": context.get("agent", ""),
        "to": to,
        "body": body,
        "groupchat": bool(context.get("groupchat", False)),
    }
    headers = {}
    if CALLBACK_TOKEN:
        headers["X-Auth-Token"] = CALLBACK_TOKEN
    try:
        resp = requests.post(callback, headers=headers, json=payload, timeout=15)
        log.info("回程投递: HTTP %s body=%s", resp.status_code, payload["body"][:100])
    except Exception as e:
        log.error("回程投递失败: %s", e)


def _pick_reply(results: list) -> str:
    for r in results:
        if r.get("ok") and r.get("answer"):
            return r["answer"]
    msgs = [r.get("message") or r.get("error") for r in results if r.get("message") or r.get("error")]
    return "；".join(msgs) if msgs else "处理完成"


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "name": "AI问答客户端执行器", "model": DEEPSEEK_CFG.get("model")}


@app.post("/api/commands")
async def api_commands(request: Request):
    """RUISI-OSCA 指令入口。兼容两种载荷：纯 JSON 数组 或 信封 {context, commands}。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体必须是合法的 JSON"})
    if isinstance(body, list):
        commands, context = body, None
    elif isinstance(body, dict):
        commands = body.get("commands", [])
        context = body.get("context")
    else:
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体必须是 JSON 数组或信封对象"})

    results = process_commands(commands, context)
    reply = _pick_reply(results)

    # 有信封且拿到结果时，通过回程通道把 AI 输出返回给睿思
    if context:
        send_callback(context, reply)

    return {"ok": True, "reply": reply, "results": results}


class ChatBody(BaseModel):
    question: str
    files: Optional[str] = None


@app.post("/api/chat")
async def direct_chat(body: ChatBody):
    """直接聊天接口（便于本地/Postman 联调）。"""
    params = {"问题": body.question, "文件": body.files}
    result = handle_ai_qa(params, None)
    return result


@app.post("/api/files/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """multipart 表单上传数据文件，返回 file_id 列表。"""
    saved = []
    errors = []
    for up in files:
        data = await up.read()
        try:
            info = file_store.save_bytes(up.filename or "upload.bin", data)
            saved.append(info)
        except Exception as e:
            errors.append({"filename": up.filename, "error": str(e)})
    return {"ok": True, "files": saved, "errors": errors}


class ImportBody(BaseModel):
    paths: List[str]


@app.post("/api/files/import")
async def import_files(body: ImportBody):
    """导入服务器本地文件路径（联调用），生成 file_id。"""
    saved = []
    errors = []
    for p in body.paths:
        try:
            info = file_store.save_file(p)
            saved.append(info)
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    return {"ok": True, "files": saved, "errors": errors}


@app.get("/api/files")
def list_files():
    found = []
    for root, _, files in os.walk(file_store.upload_dir):
        for fn in files:
            if fn.startswith("file-") and "_" in fn:
                fid = fn.split("_", 1)[0]
                found.append({"id": fid, "filename": fn.split("_", 1)[1], "path": os.path.join(root, fn)})
    found.sort(key=lambda x: x["filename"])
    return {"ok": True, "files": found}


@app.get("/api/sessions")
def list_sessions():
    return {"ok": True, "sessions": sessions.list()}


@app.delete("/api/sessions/{sid}")
def clear_session(sid: str):
    sessions.clear(sid)
    return {"ok": True, "cleared": sid}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    host = SERVER_CFG.get("host", "0.0.0.0")
    port = int(SERVER_CFG.get("port", 8009))
    log.info("AI问答客户端执行器启动中: http://%s:%s (model=%s)", host, port, DEEPSEEK_CFG.get("model"))
    uvicorn.run(app, host=host, port=port, log_level="info")
