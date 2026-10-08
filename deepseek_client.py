"""DeepSeek API 聊天客户端。

关键点：官方 DeepSeek 聊天接口的流式（SSE）响应 Content-Type 为
`text/event-stream` 且不带 charset。requests 在迭代行时如果探测不到
charset 会默认按 ISO-8859-1 (Latin-1) 解码，导致服务端返回的 UTF-8 字节
被读成乱码（例如“年”变成“å¹´”），这种坏数据在源头就产生了。

修复方式：迭代时使用 `iter_lines(decode_unicode=False)` 拿到原始字节流，
再显式按 UTF-8 解码，从根上避免 Latin-1 误解码。
"""
import json
import logging
import time

import requests

log = logging.getLogger("deepseek")


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, config: dict):
        self.base_url = (config.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "deepseek-v4-flash")
        self.thinking = (config.get("thinking") or "enabled").lower()
        self.reasoning_effort = config.get("reasoning_effort", "medium")
        self.max_tokens = int(config.get("max_tokens", 8192))
        self.stream = bool(config.get("stream", True))
        self.timeout = int(config.get("timeout_seconds", 300))
        self.max_retries = int(config.get("max_retries", 2))
        self.retry_delay = float(config.get("retry_delay_seconds", 2))
        self._session = requests.Session()

    def chat(self, messages):
        """发送一轮对话，返回助手完整回复文本。"""
        if self.stream:
            return self._chat_stream(messages)
        return self._chat_once(messages)

    def _payload(self, messages):
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": self.stream,
            "max_tokens": self.max_tokens,
        }
        if self.thinking in ("enabled", "auto", "on"):
            payload["thinking"] = {"type": "enabled"}
            if self.reasoning_effort:
                payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def _headers(self):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if self.stream else "application/json",
        }
        return headers

    def _error_message(self, resp):
        try:
            data = resp.json()
            err = data.get("error") or data
            return json.dumps(err, ensure_ascii=False)
        except Exception:
            return resp.text[:500]

    def _chat_once(self, messages):
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(messages)
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
                if resp.status_code >= 400:
                    raise DeepSeekError(
                        f"DeepSeek API 返回 HTTP {resp.status_code}: {self._error_message(resp)}"
                    )
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    raise DeepSeekError(f"DeepSeek API 响应缺少 choices: {json.dumps(data, ensure_ascii=False)[:500]}")
                return (choices[0].get("message") or {}).get("content") or ""
            except requests.exceptions.RequestException as e:
                last_err = e
                if attempt < self.max_retries:
                    log.warning("DeepSeek 请求异常（第 %s 次重试）: %s", attempt + 1, e)
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
        raise DeepSeekError(f"DeepSeek API 调用失败（已重试）: {last_err}")

    def _chat_stream(self, messages):
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(messages)
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.post(
                    url, headers=self._headers(), json=payload, stream=True, timeout=self.timeout
                )
                if resp.status_code >= 400:
                    raise DeepSeekError(
                        f"DeepSeek API 返回 HTTP {resp.status_code}: {self._error_message(resp)}"
                    )
                parts = []
                # 修复 SSE 乱码的关键一行：取原始字节，再按 UTF-8 显式解码，
                # 避免 requests 用 Latin-1 把 UTF-8 字节读成乱码。
                for raw_line in resp.iter_lines(decode_unicode=False):
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", errors="replace")
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for ch in chunk.get("choices", []) or []:
                        delta = ch.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            parts.append(content)
                return "".join(parts)
            except DeepSeekError:
                raise
            except requests.exceptions.RequestException as e:
                last_err = e
                if attempt < self.max_retries:
                    log.warning("DeepSeek 流式请求异常（第 %s 次重试）: %s", attempt + 1, e)
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
        raise DeepSeekError(f"DeepSeek API 流式调用失败（已重试）: {last_err}")
