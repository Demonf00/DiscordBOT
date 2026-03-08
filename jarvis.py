import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

# 1. 加载 .env 文件中的环境变量
load_dotenv()

# 2. 安全地获取 Token
# 如果找不到 DISCORD_BOT_TOKEN，它会报错提醒你，而不是带着空密码去裸奔
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("🚨 致命错误：在 .env 文件中找不到 DISCORD_BOT_TOKEN！")

# --- 下面是之前的核心代码 ---

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print('=================================')
    print(f'🟢 核心系统在线！已登录为: {bot.user}')
    print('✅ 已准备好接入本地 LLM 大脑...')
    print('=================================')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.channel.name != 'bot':
        return

    text = message.content 
    print(f"[{message.channel.name}] 收到老板消息：{text}")

    if text.startswith('!'):
        await bot.process_commands(message)
        return
    
    if "截图" in text or "看屏幕" in text:
        await message.channel.send("📸 收到！正在调用底层接口截取主屏幕...")
    elif "监听" in text or "找" in text:
        await message.channel.send(f"🔍 没问题，已在后台挂起任务，开始处理：**{text}**")
    else:
        await message.channel.send(f"🤖 收到指令 '{text}'。 (系统提示：本地 LLM 尚未接入)")

@bot.command()
async def ping(ctx):
    """测试连接延迟"""
    latency = round(bot.latency * 1000)
    await ctx.send(f'🏓 Pong! 当前延迟 {latency}ms。随时待命。')

# 3. 运行 Bot
bot.run(BOT_TOKEN)