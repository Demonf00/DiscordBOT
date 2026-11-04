import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from bridge import (
    SupervisorClient, TaskLedger, route_message, safe_event_label,
    clip, TASK_ID, TERMINAL, SupervisorError,
)


class RouteTests(unittest.TestCase):
    def test_natural_language_controls(self):
        self.assertEqual(route_message("查看进度"), "status")
        self.assertEqual(route_message("  现在做到哪了？"), "status")
        self.assertEqual(route_message("取消任务"), "cancel")
        self.assertEqual(route_message("暂停"), "pause")
        self.assertEqual(route_message("继续"), "resume")
        self.assertEqual(route_message("怎么用"), "help")
        self.assertEqual(route_message("有哪些任务"), "tasks")
        self.assertEqual(route_message("任务列表"), "tasks")
        self.assertEqual(route_message("重试"), "resume")
        self.assertEqual(route_message("诊断"), "health")
        self.assertEqual(route_message("进度 89abcdef"), "status")
        self.assertEqual(route_message("取消 89abcdef"), "cancel")
        self.assertEqual(route_message("帮我写暂停 89abcdef 功能"), "task")
        self.assertEqual(route_message("工作区列表"), "workspaces")
        self.assertEqual(route_message("当前工作区"), "workspace")
        self.assertEqual(route_message("切换工作区 wtagent"), "switch_workspace")
        self.assertEqual(route_message("帮我在wtagent工作区修改代码"), "task")
        self.assertIn("failed", TERMINAL)

    def test_real_requests_are_never_swallowed_by_keywords(self):
        for text in [
            "签到", "帮我做一个网站", "帮我写取消任务功能",
            "帮我看看进度查询模块为什么崩溃", "你觉得这件事怎么样",
        ]:
            self.assertEqual(route_message(text), "task")

    def test_supervisor_requires_loopback_and_secret(self):
        good = SupervisorClient("http://127.0.0.1:8765", "a" * 32, "discord")
        self.assertEqual(good.endpoint, "http://127.0.0.1:8765")
        for url in [
            "https://127.0.0.1:8765", "http://evil.example.com",
            "http://127.0.0.1@evil.com", "http://127.0.0.1:8765/private",
        ]:
            with self.assertRaises(ValueError):
                SupervisorClient(url, "a" * 32, "discord")
        with self.assertRaises(ValueError):
            SupervisorClient("http://127.0.0.1:8765", "short", "discord")

    def test_display_filters_tool_arguments_and_strips_mass_mentions(self):
        self.assertEqual(safe_event_label({
            "type": "tool.started",
            "payload": {"name": "terminal.exec", "argv": ["SECRET"]},
        }), "tool.started: terminal.exec")
        self.assertIsNone(safe_event_label({
            "type": "unknown.private",
            "payload": {"name": "SECRET"},
        }))
        self.assertEqual(safe_event_label({
            "type": "tool.started", "payload": {"toolClass": "read"},
        }), "tool.started: read")
        self.assertEqual(safe_event_label({
            "type": "tool.result_sent", "payload": {"toolClass": "other", "ok": False},
        }), "tool.result_sent: other ✗")
        self.assertIsNone(safe_event_label({
            "type": "secrets.read", "payload": {"private": "abc"},
        }))
        self.assertNotIn("@everyone", clip("hello @everyone @here"))


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_task_identity_without_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            filename = Path(temp) / "ledger.json"
            ledger = TaskLedger(filename)
            await ledger.load()
            first = "task_01234567-89ab-cdef-0123-456789abcdef"
            second = "task_11234567-89ab-cdef-0123-456789abcdef"
            await ledger.put(first, status="running", channel_id=123,
                             user_id=456, progress_message_id=789, last_seq=0)
            await ledger.put(second, status="completed", channel_id=321,
                             user_id=456, progress_message_id=790, last_seq=2, delivered=True)
            recovered = TaskLedger(filename)
            await recovered.load()
            self.assertEqual(recovered.latest(123, 456)[0], first)
            self.assertEqual(recovered.by_suffix(123, 456, first[-8:])[0], first)
            self.assertIsNone(recovered.by_suffix(123, 999, first[-8:]))
            self.assertIsNone(recovered.by_suffix(123, 456, "deadbeef"))
            self.assertIsNone(recovered.latest(123, 999))
            self.assertEqual([key for key, _ in recovered.open_tasks()], [first])
            self.assertNotIn("password", filename.read_text(encoding="utf-8").lower())
            self.assertEqual(json.loads(filename.read_text())["schema"], 1)
            self.assertEqual(recovered.workspace(123, 456, "discord"), "discord")
            await recovered.set_workspace(123, 456, "wtagent")
            again = TaskLedger(filename)
            await again.load()
            self.assertEqual(again.workspace(123, 456, "discord"), "wtagent")
            self.assertEqual(again.workspace(123, 999, "discord"), "discord")
            with self.assertRaises(ValueError):
                await again.set_workspace(123, 456, "../other")
            await again.put(first, status="failed", delivered=False)
            self.assertIn(first, [key for key, _ in again.open_tasks()])


class ClientRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_uses_selected_workspace_and_stable_idempotency_key(self):
        client = SupervisorClient("http://127.0.0.1:8765", "a" * 32, "discord")
        calls = []

        async def fake_request(method, path, *, body=None):
            calls.append((method, path, body))
            return {"task": {"taskId": "task_01234567-89ab-cdef-0123-456789abcdef"}}

        client.request = fake_request
        created = await client.create("write tests", 123456789012345, "wtagent")
        self.assertTrue(TASK_ID.fullmatch(created["taskId"]))
        self.assertEqual(calls[0][2], {
            "workspaceId": "wtagent",
            "instruction": "write tests",
            "idempotencyKey": "discord-message-123456789012345",
        })
        with self.assertRaises(ValueError):
            await client.create("x", 1, "../../secrets")

    async def test_lost_http_response_retries_same_message_not_another_task(self):
        client = SupervisorClient("http://127.0.0.1:8765", "a" * 32, "discord")
        calls = []

        async def fake_request(method, path, *, body=None):
            calls.append(body.copy())
            if len(calls) == 1:
                raise SupervisorError("无法连接本地 WTAgent Supervisor；请确认服务仍在运行。")
            return {"task": {"taskId": "task_01234567-89ab-cdef-0123-456789abcdef"}}

        client.request = fake_request
        await client.create("run tests", 12345)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    async def test_available_workspace_ids_are_validated(self):
        client = SupervisorClient("http://127.0.0.1:8765", "a" * 32, "discord")

        async def valid(*_args, **_kwargs):
            return {"workspaces": ["discord", "wtagent"]}

        client.request = valid
        self.assertEqual(await client.available_workspaces(), ["discord", "wtagent"])

        async def invalid(*_args, **_kwargs):
            return {"workspaces": ["../unsafe"]}

        client.request = invalid
        with self.assertRaises(SupervisorError):
            await client.available_workspaces()


if __name__ == "__main__":
    unittest.main()
