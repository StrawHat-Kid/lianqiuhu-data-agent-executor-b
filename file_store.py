"""上传文件 / 本地数据文件管理。

支持两种方式把数据文件纳入会话：
1. multipart 表单上传（真实上传字节流），返回 file_id；
2. JSON 导入本地已有文件路径（方便联调），同样生成 file_id。
"""
import logging
import os
import uuid
from datetime import datetime

log = logging.getLogger("file_store")


class FileStore:
    def __init__(self, config: dict):
        self.upload_dir = config.get("dir", "data/uploads")
        self.max_file_size = int(config.get("max_file_size_mb", 64)) * 1024 * 1024
        self.max_file_chars = int(config.get("max_file_chars", 500000))
        os.makedirs(self.upload_dir, exist_ok=True)

    def _persist(self, filename: str, data: bytes) -> dict:
        if len(data) > self.max_file_size:
            raise ValueError(f"文件过大（{len(data)} 字节），超过上限 {self.max_file_size} 字节")
        fid = "file-" + uuid.uuid4().hex
        safe_name = os.path.basename(filename) or "upload.bin"
        sub = datetime.now().strftime("%Y%m%d")
        folder = os.path.join(self.upload_dir, sub)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{fid}_{safe_name}")
        with open(path, "wb") as f:
            f.write(data)
        return {"id": fid, "filename": safe_name, "path": path, "size": len(data)}

    def save_bytes(self, filename: str, data: bytes) -> dict:
        return self._persist(filename, data)

    def save_file(self, path: str) -> dict:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"文件不存在: {path}")
        with open(path, "rb") as f:
            data = f.read()
        info = self._persist(os.path.basename(path), data)
        info["source"] = path
        return info

    def resolve_path(self, ref: str):
        """把用户给的引用解析为本地文件绝对路径。

        ref 可以是：
        - 本执行器上传/导入返回的 file_id（以 file- 开头）；
        - 服务器上存在的文件路径（绝对或相对路径）。
        解析不到返回 None。
        """
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref.startswith("file-"):
            for root, _, files in os.walk(self.upload_dir):
                for fn in files:
                    if fn.startswith(ref + "_"):
                        return os.path.join(root, fn)
            return None
        if os.path.isfile(ref):
            return os.path.abspath(ref)
        return None

    def read_text(self, path: str, max_chars: int = None) -> str:
        """读取文本文件内容（自动识别编码），超长自动截断并给出提示。"""
        if max_chars is None:
            max_chars = self.max_file_chars
        text = None
        for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
            try:
                with open(path, "r", encoding=enc) as f:
                    text = f.read()
                break
            except UnicodeDecodeError:
                continue
            except Exception as e:
                log.warning("读取文件失败 %s (%s): %s", path, enc, e)
        if text is None:
            with open(path, "rb") as f:
                raw = f.read()
            text = "（二进制文件，无法按文本读取，大小 %d 字节）" % len(raw)
        if len(text) > max_chars:
            text = text[: max_chars] + f"\n...[内容过长，已截断（共 {len(text)} 字符）]"
        return text
