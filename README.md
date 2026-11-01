# DiscordBOT — WTAgent Discord control

This bot has **no local language model**. It relays natural-language Discord messages to the locally running WTAgent Supervisor, which uses its configured ChatGPT web provider. The Discord bot reports actual task status, safe tool event names and model response. It never invents a successful outcome.

## Quick start on Windows

Prerequisites:
- WTAgent source checkout at `D:\Github\wtagent-selfdev` (or override `-WTAgentRoot`), Node.js installed and WTAgent's npm dependencies installed.
- Python 3 with the `py` launcher or `python` on PATH.
- Discord Bot Token from the [Discord Developer Portal](https://discord.com/developers/applications), and **Message Content Intent** enabled under Bot → Privileged Gateway Intents.
- Invite the bot to a *private* Discord channel with View Channel, Send Messages, Read Message History, and Embed Links permission. Enable Discord Developer Mode and copy your **user ID** and the **channel ID** (not names).
- Working WTAgent ChatGPT login; if needed run `node D:\Github\wtagent-selfdev\src\cli\main.js login` once from a separate terminal before starting the service.

Run from this repo's folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

The script prompts for Discord token, user ID, and channel ID on the **first run only**, generates a Supervisor bearer key, writes all secrets to ignored `.env`, sets up the Python venv, starts the local WTAgent Supervisor with the `D:\AgentWorkspace` workspace, waits for an authenticated loopback health check, then launches the Discord bot. Ctrl+C stops both processes. If WTAgent is elsewhere:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -WTAgentRoot "D:\path\to\wtagent" -WorkspaceRoot "D:\AgentWorkspace"
```

The Supervisor listens **only on 127.0.0.1:8765** and is authenticated. Never port-forward it to the internet. Never publish `.env` or `.local`. The current WTAgent browser profile is reused; avoid running two WTAgent browser sessions simultaneously against the same profile.

## Use it like normal chat

In the authorized private channel, send ordinary messages, no command prefix:

- `帮我做一个简单网站`: creates a WTAgent task, reports its ID, streams safe tool/status events, then posts the actual model result.
- `这个错误怎么解决？`: forwarded to WTAgent for a regular conversational response.
- `签到`: forwarded to WTAgent. If the site/account isn't specified, the model must ask instead of inventing a check-in.
- `进度`, `现在做到哪了`: queries your latest task.
- `取消`, `暂停`, `继续`: controls your latest task via the Supervisor API.
- `帮助`: concise help.

Each ordinary message creates a new WTAgent task, which currently means **a new model conversation**. A few recent user messages are included as context, but the bot does not pretend that separate tasks share one continuous conversation. The running task result is posted into Discord; the local durable task ledger lets it reattach after a bot restart.

**Important**: The Supervisor REST events are sanitized; Discord shows actual task/tool statuses and the final model-provided answer. The private reasoning chain and unexposed model token stream are **not** accessible and will not be fabricated as a progress display.

## Approvals

If a worker needs permission to run a tool, the status message shows **查看工具并审批**. Only the configured owner can view a private command preview and confirm **this one call** or deny it. No blanket allow-all permission is granted from Discord. Unknown operations should be refused. Some arguments are redacted before being shown, so if the intended action cannot be verified, deny and use local review instead.

Discord user ID and channel ID must be allowlisted. Non-authorized people receive no responses, but other users with access to the private channel may see the agent's replies. Keep the channel private; don't send credentials or sensitive documents there.

## Verify offline

```powershell
.\.venv\Scripts\python.exe -m unittest test_bridge -v
```

No Discord credentials or WTAgent service are required for these isolated unit tests. A true end-to-end test additionally requires your authenticated Discord bot and WTAgent/ChatGPT browser session.

## Current limitations

- No local LLM; all regular chat requests involve one WTAgent task (and potentially paid/subscription model usage).
- Tool/status feedback is supported; live thought-stream content is not exposed by the current Supervisor REST API.
- No automatic repeated task execution beyond what WTAgent's Worker can safely do; do not assume an ambiguous transaction is safe to replay.
- Only one current task is addressed by the short `进度/暂停/继续/取消` phrases per user/channel. The Supervisor itself tracks other tasks by ID.
