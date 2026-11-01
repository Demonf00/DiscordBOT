import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from bridge import (
    SupervisorClient, TaskLedger, route_message, safe_event_label,
    clip, TASK_ID,
)


class RouteTests(unittest.TestCase):
    def test_natural_language_controls(self):
        self.assertEqual(route_message("查看进度"), "status")
        self.assertEqual(route_message("  现在做到哪了？"), "status")
        self.assertEqual(route_message("取消任务"), "cancel")
        self.assertEqual(route_message("暂停"), "pause")
        self.assertEqual(route_message("继续"), "resume")
        self.assertEqual(route_message("怎么用"), "help")

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
                             user_id=456, progress_message_id=790, last_seq=2)
            recovered = TaskLedger(filename)
            await recovered.load()
            self.assertEqual(recovered.latest(123, 456)[0], first)
            self.assertIsNone(recovered.latest(123, 999))
            self.assertEqual([key for key, _ in recovered.open_tasks()], [first])
            self.assertNotIn("password", filename.read_text(encoding="utf-8").lower())
            self.assertEqual(json.loads(filename.read_text())["schema"], 1)


if __name__ == "__main__":
    unittest.main()
