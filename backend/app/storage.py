"""对象存储层：报价单原件上传后统一落存储，并通过 `stored_object` 表登记「谁在什么时候存了什么」。

设计要点：
- **内容寻址**：key = `objects/<sha256[:2]>/<sha256><ext>`，同一文件重复上传天然去重，不再多占空间。
- **后端可换**：`LocalObjectStore`（默认，本地文件系统）+ `S3ObjectStore`（预留接口，装 boto3 后补实现）。
  业务代码只依赖 `ObjectStore` 的 put/open/local_path，换后端不改调用方。
- **登记与存储分离**：存储只管字节，`stored_object` 表管元数据（原始文件名、任务/报价单归属），
  这样「报价单解析结果要留存源文件名称」与「原件可下载/预览」都能追溯到具体那次上传。
"""

import hashlib
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .config import settings

# 上传支持的格式 → MIME（预览判定也用这张表，避免依赖系统的 mime 数据库）
CONTENT_TYPES: dict[str, str] = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

DEFAULT_CONTENT_TYPE = "application/octet-stream"

# 浏览器能直接渲染的格式（其余只能下载后本地打开）
PREVIEWABLE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}


class ObjectStoreError(RuntimeError):
    """对象存储不可用（后端未实现、文件缺失等），中文信息直接反馈给用户。"""


@dataclass(frozen=True)
class StoredObject:
    """一次存储的结果描述。path 仅本地后端有值。"""

    sha256: str
    key: str
    backend: str
    size_bytes: int
    content_type: str
    original_name: str
    path: Path | None = None


def content_type_of(filename: str) -> str:
    return CONTENT_TYPES.get(Path(filename).suffix.lower(), DEFAULT_CONTENT_TYPE)


def is_previewable(filename: str) -> bool:
    return Path(filename).suffix.lower() in PREVIEWABLE_SUFFIXES


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _suffix_of(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in CONTENT_TYPES else ""


class LocalObjectStore:
    """本地文件系统对象存储：内容寻址 + 临时文件原子落盘 + 同盘硬链接省空间。"""

    backend = "local"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ---------- key ----------

    def key_for(self, sha256: str, filename: str) -> str:
        return f"objects/{sha256[:2]}/{sha256}{_suffix_of(filename)}"

    def _path_of(self, key: str) -> Path:
        return self.root / key

    # ---------- 写 ----------

    def put_file(self, src: Path, original_name: str | None = None, sha256: str | None = None) -> StoredObject:
        src = Path(src)
        name = original_name or src.name
        digest = sha256 or sha256_of_file(src)
        key = self.key_for(digest, name)
        dest = self._path_of(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            self._place(src, dest)
        return StoredObject(
            sha256=digest,
            key=key,
            backend=self.backend,
            size_bytes=dest.stat().st_size,
            content_type=content_type_of(name),
            original_name=name,
            path=dest,
        )

    def put_bytes(self, data: bytes, original_name: str) -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        key = self.key_for(digest, original_name)
        dest = self._path_of(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            tmp = tempfile.NamedTemporaryFile(dir=dest.parent, delete=False)
            try:
                tmp.write(data)
                tmp.close()
                os.replace(tmp.name, dest)
            except BaseException:
                Path(tmp.name).unlink(missing_ok=True)
                raise
        return StoredObject(
            sha256=digest,
            key=key,
            backend=self.backend,
            size_bytes=dest.stat().st_size,
            content_type=content_type_of(original_name),
            original_name=original_name,
            path=dest,
        )

    def _place(self, src: Path, dest: Path) -> None:
        """先落到同目录临时文件再 os.replace，保证 dest 要么完整要么不存在。"""
        tmp = tempfile.NamedTemporaryFile(dir=dest.parent, delete=False)
        tmp.close()
        tmp_path = Path(tmp.name)
        try:
            try:
                os.unlink(tmp_path)
                os.link(src, tmp_path)  # 同盘硬链接：不额外占用空间
            except OSError:
                shutil.copy2(src, tmp_path)
            os.replace(tmp_path, dest)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # ---------- 读 ----------

    def exists(self, key: str) -> bool:
        return self._path_of(key).is_file()

    def open(self, key: str) -> BinaryIO:
        path = self._path_of(key)
        if not path.is_file():
            raise ObjectStoreError(f"对象存储中找不到文件：{key}")
        return path.open("rb")

    def local_path(self, key: str) -> Path | None:
        path = self._path_of(key)
        return path if path.is_file() else None

    def delete(self, key: str) -> None:
        self._path_of(key).unlink(missing_ok=True)


class S3ObjectStore:
    """S3 / MinIO 兼容对象存储（预留）。

    接口已按 `LocalObjectStore` 的形状留好，接入时：`pip install boto3`，把下面的
    `NotImplementedError` 换成 boto3 client 调用（put_object / get_object / head_object）即可，
    调用方（上传钩子、源文件下载接口）不需要改动。
    """

    backend = "s3"

    def __init__(self, bucket: str | None = None, endpoint: str | None = None) -> None:
        self.bucket = bucket or os.environ.get("OBJECT_STORE_BUCKET") or ""
        self.endpoint = endpoint or os.environ.get("OBJECT_STORE_ENDPOINT") or ""

    def key_for(self, sha256: str, filename: str) -> str:
        return f"objects/{sha256[:2]}/{sha256}{_suffix_of(filename)}"

    def _unsupported(self, action: str):
        raise ObjectStoreError(
            f"对象存储后端 s3 尚未接入（{action}）：请安装 boto3 并补齐 S3ObjectStore 实现，"
            f"或改用 OBJECT_STORE_BACKEND=local。"
        )

    def put_file(self, src: Path, original_name: str | None = None, sha256: str | None = None) -> StoredObject:
        self._unsupported("上传")

    def put_bytes(self, data: bytes, original_name: str) -> StoredObject:
        self._unsupported("上传")

    def exists(self, key: str) -> bool:
        self._unsupported("探测")

    def open(self, key: str) -> BinaryIO:
        self._unsupported("读取")

    def local_path(self, key: str) -> Path | None:
        return None  # 远程后端没有本地路径，下载走 open()

    def delete(self, key: str) -> None:
        self._unsupported("删除")


_store = None


def get_store():
    """当前对象存储后端（进程内单例，按配置懒加载）。"""
    global _store
    if _store is None:
        backend = settings.object_store_backend
        if backend == "s3":
            _store = S3ObjectStore()
        else:
            _store = LocalObjectStore(settings.object_store_dir)
    return _store


def set_store(store) -> None:
    """替换对象存储后端（测试注入临时目录用）。传 None 恢复按配置重建。"""
    global _store
    _store = store


# ---------- stored_object 登记（存储元数据与归属关系） ----------


def record_object(
    conn: sqlite3.Connection,
    stored: StoredObject,
    original_name: str | None = None,
    task_id: int | None = None,
    quote_id: int | None = None,
) -> int:
    """登记一次上传/接入：同一内容可能被多次上传，每次上传各留一条（原始文件名按次留存）。"""
    cur = conn.execute(
        """INSERT INTO stored_object
               (sha256, object_key, backend, size_bytes, content_type, original_name, task_id, quote_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            stored.sha256,
            stored.key,
            stored.backend,
            stored.size_bytes,
            stored.content_type,
            original_name or stored.original_name,
            task_id,
            quote_id,
        ),
    )
    return cur.lastrowid


def store_and_record(
    conn: sqlite3.Connection,
    path: Path,
    original_name: str | None = None,
    task_id: int | None = None,
    quote_id: int | None = None,
    sha256: str | None = None,
) -> StoredObject:
    """存放文件并登记。已有同内容对象时只补登记，不重复写字节。"""
    stored = get_store().put_file(Path(path), original_name=original_name, sha256=sha256)
    record_object(conn, stored, original_name=original_name, task_id=task_id, quote_id=quote_id)
    return stored


def find_object_for_quote(conn: sqlite3.Connection, quote_id: int) -> sqlite3.Row | None:
    """该报价单最近一次上传的原件登记（源文件名称 / 下载预览入口都靠它）。"""
    return conn.execute(
        "SELECT * FROM stored_object WHERE quote_id = ? ORDER BY id DESC LIMIT 1", (quote_id,)
    ).fetchone()


def find_object_by_sha(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    """按内容哈希找登记记录（报价单只有 file_hash、没有 quote_id 归属时的回退）。"""
    return conn.execute(
        "SELECT * FROM stored_object WHERE sha256 = ? ORDER BY id DESC LIMIT 1", (sha256,)
    ).fetchone()
