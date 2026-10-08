"""多会话对话历史存储（进程内存）。"""
import threading


class SessionStore:
    def __init__(self, config: dict):
        self.max_history = int(config.get("max_history", 20))
        self._lock = threading.Lock()
        self._sessions = {}

    def get(self, sid: str):
        with self._lock:
            return list(self._sessions.get(sid, []))

    def set(self, sid: str, messages):
        with self._lock:
            if len(messages) > self.max_history * 2 + 1:
                messages = messages[:1] + messages[-(self.max_history * 2):]
            self._sessions[sid] = list(messages)

    def clear(self, sid: str):
        with self._lock:
            self._sessions.pop(sid, None)

    def list(self):
        with self._lock:
            return {k: len(v) for k, v in self._sessions.items()}
