"""Owner-scoped temporary files and explicitly persisted browser login state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .browser_errors import AgentBrowserError

Owner = tuple[str, str]
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_OWNER_BYTES = 200 * 1024 * 1024
MAX_OWNER_FILES = 20
MAX_STATE_BYTES = 20 * 1024 * 1024


def owner_key(owner: Owner) -> str:
    return hashlib.sha256(json.dumps(owner, ensure_ascii=False).encode()).hexdigest()


def safe_name(name: str) -> str:
    return re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")[:150] or "file"


@dataclass
class BrowserFile:
    id: str
    name: str
    source: str
    path: Path
    status: str = "downloading"
    size: int = 0
    error: str = ""
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def describe(self) -> dict[str, Any]:
        return {
            "file_id": self.id,
            "name": self.name,
            "source": self.source,
            "status": self.status,
            "size": self.size,
            "error": self.error,
        }


class BrowserStorage:
    """Keep model-visible IDs separate from server-controlled filesystem paths.

    Files live until plugin unload; saved login state lives in data_dir until an
    explicit clear. A temporary root is used when no persistent root is supplied.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir
        self._temporary_root: tempfile.TemporaryDirectory | None = None
        self._file_root: tempfile.TemporaryDirectory | None = None
        self.files: dict[Owner, dict[str, BrowserFile]] = {}
        self.locks: dict[Owner, asyncio.Lock] = {}
        self.imports: dict[tuple[Owner, str], str] = {}

    def lock(self, owner: Owner) -> asyncio.Lock:
        return self.locks.setdefault(owner, asyncio.Lock())

    def root(self) -> Path:
        if self.data_dir is None:
            self._temporary_root = tempfile.TemporaryDirectory(prefix="browser-state-")
            self.data_dir = Path(self._temporary_root.name)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir

    def reserve(self, owner: Owner, name: str, source: str) -> BrowserFile:
        files = self.files.setdefault(owner, {})
        if (
            len(files) >= MAX_OWNER_FILES
            or sum(f.size for f in files.values()) >= MAX_OWNER_BYTES
        ):
            raise AgentBrowserError(
                "FILE_LIMIT",
                "个人文件区已满（最多 20 个文件、合计 200 MiB），请先删除文件。",
            )
        if self._file_root is None:
            self._file_root = tempfile.TemporaryDirectory(
                prefix="files-", dir=self.root()
            )
        folder = Path(self._file_root.name) / owner_key(owner)
        folder.mkdir(exist_ok=True)
        file_id = "file_" + uuid.uuid4().hex
        item = BrowserFile(file_id, safe_name(name), source, folder / file_id)
        files[file_id] = item
        return item

    def get(self, owner: Owner, file_id: str, *, ready: bool = False) -> BrowserFile:
        item = self.files.get(owner, {}).get(file_id)
        if item is None:
            raise AgentBrowserError(
                "FILE_NOT_FOUND", "当前用户的文件编号不存在，请先列出文件。"
            )
        if ready and (item.status != "ready" or not item.path.is_file()):
            raise AgentBrowserError(
                "FILE_NOT_READY", "文件尚未就绪或下载失败，请查询文件状态。"
            )
        return item

    def finish(self, owner: Owner, item: BrowserFile) -> None:
        size = item.path.stat().st_size
        total = sum(f.size for f in self.files.get(owner, {}).values() if f is not item)
        if size > MAX_FILE_BYTES or total + size > MAX_OWNER_BYTES:
            raise AgentBrowserError(
                "FILE_LIMIT", "文件超过 50 MiB，或个人文件总量超过 200 MiB。"
            )
        item.size, item.status = size, "ready"
        item.done.set()

    def fail(self, item: BrowserFile, message: str) -> None:
        item.status, item.error = "failed", message
        try:
            item.path.unlink(missing_ok=True)
        except OSError:
            item.error += " 临时文件清理失败，请删除该文件或检查目录权限。"
            try:
                item.size = item.path.stat().st_size
            except OSError:
                item.size = MAX_FILE_BYTES
        finally:
            item.done.set()

    async def import_file(
        self, owner: Owner, path: Path, name: str, key: str
    ) -> BrowserFile:
        """Import an adapter-resolved attachment, never a model-supplied path."""
        async with self.lock(owner):
            existing = self.imports.get((owner, key))
            if existing in self.files.get(owner, {}):
                return self.get(owner, existing, ready=True)
            if not path.is_file():
                raise AgentBrowserError("ATTACHMENT_UNAVAILABLE", "聊天附件无法读取。")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise AgentBrowserError(
                    "FILE_LIMIT", "聊天附件超过单文件 50 MiB 上限。"
                )
            item = self.reserve(owner, name, "attachment")
            try:
                with path.open("rb") as source, item.path.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
                        if target.tell() > MAX_FILE_BYTES:
                            raise AgentBrowserError(
                                "FILE_LIMIT", "聊天附件超过 50 MiB 上限。"
                            )
                        await asyncio.sleep(0)
                self.finish(owner, item)
                self.imports[(owner, key)] = item.id
                return item
            except BaseException:
                self.fail(item, "附件导入失败。")
                if not item.path.exists():
                    self.files[owner].pop(item.id, None)
                raise

    def delete(self, owner: Owner, file_id: str) -> None:
        item = self.get(owner, file_id)
        if item.status == "downloading":
            raise AgentBrowserError("FILE_NOT_READY", "请等待下载完成或失败后再删除。")
        item.path.unlink(missing_ok=True)
        del self.files[owner][file_id]
        self.imports = {
            key: value for key, value in self.imports.items() if value != file_id
        }

    def state_path(self, owner: Owner) -> Path:
        folder = self.root() / "login_states"
        folder.mkdir(exist_ok=True)
        return folder / (owner_key(owner) + ".json")

    def read_state(self, owner: Owner) -> dict[str, Any]:
        path = self.state_path(owner)
        if not path.is_file():
            raise AgentBrowserError("STATE_NOT_FOUND", "当前用户没有已保存的登录状态。")
        try:
            if path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("state too large")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"cookies", "origins"}:
                raise ValueError("invalid state")
            if not isinstance(data["cookies"], list) or not isinstance(
                data["origins"], list
            ):
                raise ValueError("invalid state")
            return data
        except (ValueError, OSError) as exc:
            raise AgentBrowserError(
                "INVALID_STATE", "保存的登录状态损坏或过大，请清除后重新保存。"
            ) from exc

    def write_state(self, owner: Owner, data: dict[str, Any]) -> None:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(content) > MAX_STATE_BYTES:
            raise AgentBrowserError("STATE_LIMIT", "登录状态超过 20 MiB，未保存。")
        path = self.state_path(owner)
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("wb") as output:
                output.write(content)
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        if self._file_root is not None:
            self._file_root.cleanup()
        if self._temporary_root is not None:
            self._temporary_root.cleanup()
        self.files.clear()
        self.imports.clear()
