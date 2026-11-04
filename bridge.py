"""Discord -> WTAgent loopback Supervisor bridge. No local model or fake results."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

TERMINAL = {"completed", "failed", "cancelled", "blocked_uncertain"}
AWAITING = {"waiting_approval", "paused", "blocked_uncertain"}
STATUS_WORDS = frozenset({
    "进度", "查看进度", "看看进度", "帮我看看进度", "任务进度", "现在做到哪了",
    "怎么样了", "做到哪了", "status", "progress",
})
CANCEL_WORDS = frozenset({"取消", "取消任务", "停止", "停止任务", "停下", "别做了", "cancel"})
PAUSE_WORDS = frozenset({"暂停", "暂停任务", "pause"})
RESUME_WORDS = frozenset({"继续", "继续任务", "恢复任务", "重试", "重试任务", "resume", "retry"})
HELP_WORDS = frozenset({"帮助", "怎么用", "help", "你能做什么"})
HEALTH_WORDS = frozenset({"诊断", "健康检查", "系统状态", "health", "doctor"})
LIST_WORDS = frozenset({"任务列表", "有哪些任务", "查看任务", "列出任务", "所有任务", "tasks", "list tasks"})
WORKSPACE_WORDS = frozenset({"当前工作区", "当前项目", "使用哪个工作区", "current workspace"})
WORKSPACES_WORDS = frozenset({"工作区列表", "有哪些工作区", "列出工作区", "workspaces"})
SWITCH_WORKSPACE = re.compile(r"^(?:切换(?:到)?工作区|使用工作区|workspace)\s*[:：]?\s*([a-zA-Z0-9_-]{1,64})$", re.I)
TASK_ID = re.compile(r"^task_[a-f0-9-]{36}$")
CONTROL_WITH_ID = re.compile(
    r"^(进度|查看进度|取消|暂停|继续|重试|status|cancel|pause|resume|retry)\s+([a-f0-9]{8})$",
    re.I,
)
MAX_REQUEST = 8000


def route_message(message: str) -> str:
    """Only *entire* control phrases are intercepted; everything else goes to WTAgent."""
    text = " ".join(message.strip().lower().split()).strip("。！？!? ")
    matched = CONTROL_WITH_ID.fullmatch(text)
    if matched:
        verb = matched.group(1)
        return {
            "进度": "status", "查看进度": "status", "status": "status",
            "取消": "cancel", "cancel": "cancel",
            "暂停": "pause", "pause": "pause",
            "继续": "resume", "重试": "resume", "resume": "resume", "retry": "resume",
        }[verb]
    if text in HEALTH_WORDS:
        return "health"
    if text in WORKSPACE_WORDS:
        return "workspace"
    if text in WORKSPACES_WORDS:
        return "workspaces"
    if SWITCH_WORKSPACE.fullmatch(text):
        return "switch_workspace"
    if text in STATUS_WORDS:
        return "status"
    if text in CANCEL_WORDS:
        return "cancel"
    if text in PAUSE_WORDS:
        return "pause"
    if text in RESUME_WORDS:
        return "resume"
    if text in HELP_WORDS:
        return "help"
    if text in LIST_WORDS:
        return "tasks"
    return "task"


class SupervisorError(Exception):
    pass


class SupervisorClient:
    def __init__(self, endpoint: str, token: str, workspace_id: str):
        parts = urlsplit(endpoint)
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in {"", "/"}):
            raise ValueError("Supervisor URL must be a plain loopback HTTP address.")
        if len(token.encode("utf-8")) < 32:
            raise ValueError("WTAgent Supervisor token must be >= 32 bytes.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", workspace_id):
            raise ValueError("Invalid workspace id.")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.workspace_id = workspace_id
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12),
            headers={"Authorization": f"Bearer {self.token}"},
            trust_env=False,
        )
        return self

    async def __aexit__(self, *_):
        if self.session:
            await self.session.close()

    async def request(self, method: str, path: str, *, body=None):
        if not self.session:
            raise SupervisorError("Supervisor connection is not started.")
        try:
            async with self.session.request(method, self.endpoint + path, json=body) as response:
                if response.status >= 400:
                    # Do not quote the response body; it may contain private runtime details.
                    raise SupervisorError(f"WTAgent API HTTP {response.status} ({path.split('?')[0]}).")
                data = await response.json(content_type=None)
                if not isinstance(data, dict):
                    raise SupervisorError("Unexpected WTAgent API response.")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise SupervisorError("无法连接本地 WTAgent Supervisor；请确认服务仍在运行。") from error

    async def health(self):
        data = await self.request("GET", "/v1/health")
        if data.get("ok") is not True or not isinstance(data.get("counts"), dict):
            raise SupervisorError("Invalid WTAgent health response.")
        return data

    async def available_workspaces(self):
        data = await self.request("GET", "/v1/workspaces")
        ids = data.get("workspaces")
        if not isinstance(ids, list) or any(
            not isinstance(item, str) or
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item)
            for item in ids
        ):
            raise SupervisorError("WTAgent returned invalid workspace ids.")
        return ids

    async def create(self, instruction: str, message_id: int, workspace_id: str | None = None):
        if not instruction.strip() or len(instruction.encode("utf-8")) > MAX_REQUEST:
            raise ValueError("消息过长或为空。")
        selected = workspace_id or self.workspace_id
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", selected):
            raise ValueError("Invalid workspace id.")
        # Retries reuse the SAME idempotency key. The Supervisor durably
        # deduplicates a request even if the first HTTP response was lost.
        body = {
            "workspaceId": selected,
            "instruction": instruction,
            "idempotencyKey": f"discord-message-{message_id}",
        }
        try:
            data = await self.request("POST", "/v1/tasks", body=body)
        except SupervisorError as error:
            if "无法连接本地 WTAgent" not in str(error):
                raise
            await asyncio.sleep(0.25)
            data = await self.request("POST", "/v1/tasks", body=body)
        return data["task"]

    async def get(self, task_id: str):
        if not TASK_ID.fullmatch(task_id):
            raise ValueError("Invalid task id.")
        return (await self.request("GET", f"/v1/tasks/{task_id}"))["task"]

    async def events(self, task_id: str, after: int):
        if not TASK_ID.fullmatch(task_id) or not isinstance(after, int) or after < 0:
            raise ValueError("Invalid event request.")
        data = await self.request("GET", f"/v1/tasks/{task_id}/events?after={after}")
        return data.get("events", [])

    async def action(self, task_id: str, operation: str):
        if not TASK_ID.fullmatch(task_id) or operation not in {"pause", "resume", "cancel"}:
            raise ValueError("Invalid task operation.")
        return (await self.request("POST", f"/v1/tasks/{task_id}/{operation}"))["task"]

    async def decide(self, task_id: str, request_id: str, approved: bool):
        if not TASK_ID.fullmatch(task_id) or not re.fullmatch(r"[a-f0-9]{64}", request_id):
            raise ValueError("Invalid approval id.")
        return (await self.request(
            "POST", f"/v1/tasks/{task_id}/approvals/{request_id}/decision",
            body={"decision": "approve" if approved else "deny"},
        ))["task"]


class TaskLedger:
    """State only; never store chat content, model output, tool arguments or secrets."""

    def __init__(self, filename: Path):
        self.filename = Path(filename)
        self.lock = asyncio.Lock()
        self.items: dict[str, dict] = {}
        self.workspace_preferences: dict[str, str] = {}

    async def load(self):
        if not self.filename.exists():
            self.items = {}
            self.workspace_preferences = {}
            return
        raw = json.loads(self.filename.read_text(encoding="utf-8"))
        if raw.get("schema") != 1 or not isinstance(raw.get("tasks"), dict):
            raise ValueError("Invalid persisted Discord task state.")
        self.items = {key: value for key, value in raw["tasks"].items()
                      if TASK_ID.fullmatch(key) and isinstance(value, dict)}
        prefs = raw.get("workspace_preferences", {})
        if not isinstance(prefs, dict):
            raise ValueError("Invalid workspace preference state.")
        self.workspace_preferences = {
            key: value for key, value in prefs.items()
            if re.fullmatch(r"[0-9]+:[0-9]+", key)
            and isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
        }

    async def save(self):
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="ledger-", suffix=".tmp", dir=self.filename.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"schema": 1, "tasks": self.items,
                           "workspace_preferences": self.workspace_preferences},
                          handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.filename)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    async def put(self, task_id, **fields):
        async with self.lock:
            self.items[task_id] = {**self.items.get(task_id, {}), **fields}
            await self.save()

    def workspace(self, channel_id: int, user_id: int, default: str) -> str:
        return self.workspace_preferences.get(f"{channel_id}:{user_id}", default)

    async def set_workspace(self, channel_id: int, user_id: int, workspace_id: str):
        if not isinstance(workspace_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", workspace_id
        ):
            raise ValueError("Invalid workspace id.")
        async with self.lock:
            self.workspace_preferences[f"{channel_id}:{user_id}"] = workspace_id
            await self.save()

    def latest(self, channel_id: int, user_id: int):
        entries = [(key, item) for key, item in self.items.items()
                   if item.get("channel_id") == channel_id and item.get("user_id") == user_id]
        if not entries:
            return None
        # Insertion order survives JSON persistence; don't fall back to someone else's tasks.
        return entries[-1]

    def by_suffix(self, channel_id: int, user_id: int, suffix: str):
        if not re.fullmatch(r"[a-f0-9]{8}", suffix):
            return None
        matches = [(key, item) for key, item in self.items.items()
                   if key.endswith(suffix)
                   and item.get("channel_id") == channel_id
                   and item.get("user_id") == user_id]
        # Never assume a shortened ID is unique if there is a collision.
        return matches[0] if len(matches) == 1 else None

    def open_tasks(self):
        return [(key, item) for key, item in self.items.items()
                if item.get("status") is not None
                and (item.get("status") not in TERMINAL or not item.get("delivered"))]


def safe_event_label(event: dict) -> str | None:
    """Only display allowlisted tool names/status, never contents or arguments."""
    if not isinstance(event, dict):
        return None
    kind = event.get("type", "")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    if kind in {"task.started", "task.completed", "task.failed", "task.paused",
                "task.waiting_approval", "task.waiting_retry"}:
        return kind.removeprefix("task.").replace("_", " ")
    if kind.startswith("tool."):
        name = payload.get("name")
        if isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", name):
            return f"{kind}: {name}"
        tool_class = payload.get("toolClass")
        if tool_class in {"read", "verify", "other", "unknown"}:
            outcome = ""
            if kind == "tool.result_sent" and isinstance(payload.get("ok"), bool):
                outcome = " ✓" if payload["ok"] else " ✗"
            return f"{kind}: {tool_class}{outcome}"
        return kind
    if kind in {"model.message_complete", "runtime.initializing", "run.completed",
                "model.message_sent", "protocol.invalid"}:
        return kind
    return None


def clip(message: str, max_chars: int = 1800) -> str:
    text = str(message).replace("@everyone", "@ everyone").replace("@here", "@ here")
    return text[:max_chars] + ("…" if len(text) > max_chars else "")
