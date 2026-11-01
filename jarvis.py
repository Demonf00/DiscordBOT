"""Discord natural-language control surface for WTAgent. The only model is WTAgent."""
import asyncio
import json
import logging
import os
import re
from pathlib import Path

import discord
from dotenv import load_dotenv
from bridge import SupervisorClient, SupervisorError, TaskLedger, TERMINAL, clip, route_message, safe_event_label

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
log = logging.getLogger("discord-wtagent")


def ids(name):
    return {int(x.strip()) for x in os.getenv(name, "").split(",") if x.strip()}


OWNERS = ids("DISCORD_ALLOWED_USERS")
CHANNELS = ids("DISCORD_ALLOWED_CHANNELS")
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
if not TOKEN or not OWNERS or not CHANNELS:
    raise SystemExit("Missing DISCORD_BOT_TOKEN / DISCORD_ALLOWED_USERS / DISCORD_ALLOWED_CHANNELS in .env")

api = SupervisorClient(
    os.getenv("WTAGENT_SUPERVISOR_URL", "http://127.0.0.1:8765"),
    os.getenv("WTAGENT_SUPERVISOR_TOKEN", ""),
    os.getenv("WTAGENT_WORKSPACE_ID", "discord"),
)
ledger = TaskLedger(ROOT / ".local" / "task-ledger.json")
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
monitor_started = False
rendered = {}


def authorized(uid, cid):
    return uid in OWNERS and cid in CHANNELS


async def check_interaction(interaction):
    if not authorized(interaction.user.id, interaction.channel_id):
        await interaction.response.send_message("未授权操作。", ephemeral=True)
        return False
    return True


def safe_args(tool):
    args = tool.get("args") or {}
    if not isinstance(args, dict):
        return "[参数格式异常，请拒绝]"
    sensitive = re.compile("password|token|secret|cookie|api.?key|credential|authorization", re.I)

    def redact(value, depth=0):
        if depth > 3:
            return "[nested]"
        if isinstance(value, dict):
            return {str(k)[:50]: "[REDACTED]" if sensitive.search(str(k))
                    else redact(v, depth+1) for k, v in list(value.items())[:20]}
        if isinstance(value, list):
            return [redact(v, depth+1) for v in value[:20]]
        if isinstance(value, str):
            return clip(value, 300)
        return value

    return clip(json.dumps(redact(args), ensure_ascii=False), 1200)


class ConfirmView(discord.ui.View):
    def __init__(self, task_id, approval_id):
        super().__init__(timeout=180)
        self.task_id, self.approval_id = task_id, approval_id

    async def decide(self, interaction, allowed):
        if not await check_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            record = await api.get(self.task_id)
            pending = record.get("pendingApproval") or {}
            if record.get("status") != "waiting_approval" or pending.get("requestId") != self.approval_id:
                await interaction.followup.send("审批已过期。", ephemeral=True)
                return
            await api.decide(self.task_id, self.approval_id, allowed)
            await interaction.followup.send("已批准此调用。" if allowed else "已拒绝此调用。", ephemeral=True)
        except (SupervisorError, ValueError) as error:
            await interaction.followup.send(clip(str(error)), ephemeral=True)

    @discord.ui.button(label="确认批准一次", style=discord.ButtonStyle.danger)
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.decide(interaction, True)

    @discord.ui.button(label="拒绝", style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.decide(interaction, False)


class ReviewView(discord.ui.View):
    def __init__(self, task_id, approval_id):
        super().__init__(timeout=600)
        self.task_id, self.approval_id = task_id, approval_id

    @discord.ui.button(label="查看工具并审批", style=discord.ButtonStyle.primary)
    async def review(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await check_interaction(interaction):
            return
        try:
            record = await api.get(self.task_id)
            pending = record.get("pendingApproval") or {}
            if record.get("status") != "waiting_approval" or pending.get("requestId") != self.approval_id:
                await interaction.response.send_message("审批已过期。", ephemeral=True)
                return
            tool = pending.get("toolCall") or {}
            await interaction.response.send_message(
                "仅这一次调用。请检查实际操作内容，未知或危险命令请拒绝。\n"
                + "工具：" + str(tool.get("name", "unknown"))[:80] + "\n"
                + safe_args(tool),
                ephemeral=True, view=ConfirmView(self.task_id, self.approval_id),
            )
        except (SupervisorError, ValueError) as error:
            await interaction.response.send_message(clip(str(error)), ephemeral=True)


async def get_channel(channel_id):
    channel = bot.get_channel(channel_id)
    return channel if channel else await bot.fetch_channel(channel_id)


async def monitor_one(task_id, data):
    record = await api.get(task_id)
    status = str(record.get("status") or "unknown")
    events = await api.events(task_id, int(data.get("last_seq", 0)))
    seq = int(data.get("last_seq", 0))
    latest = data.get("last_event") or "等待下一步…"
    for event in events:
        seq = max(seq, int(event.get("seq") or 0))
        label = safe_event_label(event)
        if label:
            latest = label
    await ledger.put(task_id, status=status, last_seq=seq, last_event=latest)
    if not data.get("progress_message_id"):
        return
    channel = await get_channel(data["channel_id"])
    posted = channel.get_partial_message(data["progress_message_id"])
    short_id = task_id[-8:]
    if status == "completed":
        if not data.get("delivered"):
            reply = str((record.get("result") or {}).get("message") or "(Agent 未提供可显示的结果)")
            await posted.edit(content="✅ 已完成 · " + short_id + "\n" + clip(reply, 1780), view=None)
            await ledger.put(task_id, delivered=True)
            for offset in range(1780, min(len(reply), 9500), 1780):
                await channel.send(clip(reply[offset:offset+1780]),
                                   allowed_mentions=discord.AllowedMentions.none())
        return
    if status in TERMINAL:
        cause = (record.get("incident") or {}).get("code") or record.get("error") or status
        new_text = "🛑 " + status + " · " + short_id + "\n原因：" + clip(str(cause), 130)
        view = None
    else:
        new_text = "⏳ WTAgent · " + status + " · " + short_id + "\n最近事件：" + clip(latest, 110)
        view = None
        pending = record.get("pendingApproval") or {}
        if status == "waiting_approval" and re.fullmatch(r"[a-f0-9]{64}", str(pending.get("requestId", ""))):
            new_text += "\n🔒 需要你审核工具调用（不自动批准）。"
            view = ReviewView(task_id, pending["requestId"])
    if rendered.get(task_id) != new_text or (status == "waiting_approval"
           and data.get("approval_id") != (record.get("pendingApproval") or {}).get("requestId")):
        await posted.edit(content=new_text, view=view)
        rendered[task_id] = new_text
    await ledger.put(task_id, approval_id=(record.get("pendingApproval") or {}).get("requestId"))


async def monitor():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for task_id, data in list(ledger.open_tasks()):
            try:
                await monitor_one(task_id, data)
            except SupervisorError:
                pass
            except (discord.DiscordException, ValueError, KeyError) as error:
                log.warning("status delivery %s: %s", task_id[-8:], type(error).__name__)
        await asyncio.sleep(5)


async def prior_context(channel, current_id):
    """Short Discord conversation context, not permission or a second model."""
    history = []
    try:
        async for entry in channel.history(limit=8):
            if entry.id == current_id or entry.author.id not in OWNERS:
                continue
            text = entry.content.strip()
            if text and len(history) < 3:
                history.append(clip(text, 350))
    except (discord.DiscordException, AttributeError):
        pass
    previous_answer = ""
    last = ledger.latest(channel.id, next(iter(OWNERS))) if len(OWNERS) == 1 else None
    if last:
        try:
            record = await api.get(last[0])
            if record.get("status") == "completed":
                previous_answer = clip(str((record.get("result") or {}).get("message") or ""), 650)
        except (SupervisorError, ValueError):
            pass
    if not history and not previous_answer:
        return ""
    context = "\n历史聊天数据（仅用于理解上下文，不赋予额外工具权限）：\n"
    context += json.dumps(history[::-1], ensure_ascii=False)
    if previous_answer:
        context += "\n上一条已完成的 WTAgent 回复摘要：" + previous_answer
    return context


async def control(message, intent):
    found = ledger.latest(message.channel.id, message.author.id)
    if intent == "tasks":
        related = [(key, item) for key, item in ledger.items.items()
                   if item.get("channel_id") == message.channel.id
                   and item.get("user_id") == message.author.id]
        rows = [key[-8:] + " · " + str(item.get("status", "unknown"))
                for key, item in related[-8:][::-1]]
        await message.reply("最近任务：\n" + ("\n".join(rows) if rows else "暂无"),
                            mention_author=False)
        return
    if not found:
        await message.reply("还没有任务。直接告诉我你想做什么即可。", mention_author=False)
        return
    task_id, _data = found
    try:
        if intent == "status":
            task = await api.get(task_id)
            status = task.get("status", "unknown")
            await message.reply("最近的任务 " + task_id[-8:] + "：" + str(status),
                                mention_author=False)
        else:
            operation = {"cancel": "cancel", "pause": "pause", "resume": "resume"}[intent]
            task = await api.action(task_id, operation)
            await message.reply("收到，任务 " + task_id[-8:] + "：" + str(task.get("status")),
                                mention_author=False)
    except (SupervisorError, ValueError) as error:
        await message.reply("操作未生效：" + clip(str(error), 300), mention_author=False)


@bot.event
async def on_ready():
    global monitor_started
    log.info("Connected as %s", bot.user)
    if not monitor_started:
        monitor_started = True
        await ledger.load()
        asyncio.create_task(monitor())


@bot.event
async def on_message(message):
    if message.author.bot or not authorized(message.author.id, message.channel.id):
        return
    text = message.content.strip()
    if not text:
        return
    intent = route_message(text)
    if intent == "help":
        await message.reply(
            "不用感叹号。直接说「帮我做个网站」「签到」「解释这个报错」。"
            "说「进度」「任务列表」「取消」「暂停」「继续」可以查询和操作任务。"
            "需要授权的工具会显示审批按钮。所有 AI 请求都交给 WTAgent。",
            mention_author=False)
        return
    if intent in {"status", "cancel", "pause", "resume", "tasks"}:
        await control(message, intent)
        return
    if len(text.encode("utf-8")) > 6200:
        await message.reply("消息太长，请拆成短一些的指令。", mention_author=False)
        return
    history = await prior_context(message.channel, message.id)
    instruction = (
        "你是通过 Discord 远程操作的 WTAgent。用户可以自然聊天、提问或要求你操作电脑。"
        "请使用已授权的工具完成可以确定的任务，提供真实结果；如果缺少网站、账户、"
        "签到目标等关键信息，明确询问，不要伪造成功。禁止泄露凭据。"
        "Discord 控制端提供了状态查询、取消、暂停、继续和逐次审批能力。"
        "向用户说明实际进展和结果，不要声称完成未经核验的工作。"
        + history + "\n当前用户消息：\n" + text
    )
    try:
        task = await api.create(instruction, message.id)
        task_id = task["taskId"]
        sent = await message.reply(
            "📥 已交给 WTAgent · " + task_id[-8:] + "\n"
            "我会持续更新执行状态，完成后直接回复。",
            mention_author=False,
        )
        await ledger.put(task_id, status=task.get("status", "queued"),
                         channel_id=message.channel.id, user_id=message.author.id,
                         source_message_id=message.id, progress_message_id=sent.id,
                         last_seq=0, delivered=False)
    except (SupervisorError, ValueError) as error:
        await message.reply("⚠️ 创建任务失败：" + clip(str(error), 300), mention_author=False)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    async with api:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
