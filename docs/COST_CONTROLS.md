# Cost Controls

## Default posture (2026-05-16)

**The Anthropic API key has been removed from `.env`.** All LLM calls now
route through the Claude Code OAuth fallback (Max plan, **$0 marginal**).
`chat.py::chat()` auto-detects: if `ANTHROPIC_API_KEY` is empty/missing
AND a Claude Code OAuth token is reachable in the macOS keychain, it
uses the OAuth path with the `oauth-2025-04-20` beta header.

This means:
- `/api/chat` (SPA "Ask reip" tab) → OAuth → $0
- `reip digest` (weekly cron) → OAuth → $0
- `reip mcp` (the MCP server) → never calls Anthropic; reuses Claude
  Code's session via the agent that called it → $0
- The tripwire cap is set to **`CHAT_DAILY_BUDGET_USD=0.10`** in `.env`
  — if a key is ever re-added and starts burning budget, calls refuse
  after $0.10/day. Defense in depth.

**Best everyday path: use Claude Code with the reip MCP server.** Same
17 tools, native chat, zero extra cost. The SPA chat tab is kept for
parity but routes through the same OAuth path.

The chat surface burns Anthropic credits IF an API key is present. The
platform has three layers of defense for that case:

## 1. Hard daily spend cap

`/api/chat` reads today's logged usage from `~/.reip/chat_usage.jsonl` and
refuses calls when the daily total exceeds `CHAT_DAILY_BUDGET_USD`
(default **$2.00/day**). Configurable per-environment:

```bash
# Strict — $0.50/day, ~4 chat conversations
CHAT_DAILY_BUDGET_USD=0.50 reip serve

# Generous — $10/day for heavy days
CHAT_DAILY_BUDGET_USD=10.00 reip serve

# Disabled — no cap (don't do this)
CHAT_DAILY_BUDGET_USD=0 reip serve
```

When blocked, `/api/chat` returns:

```json
{"error":"Daily chat budget hit: $2.03 spent today, cap $2.00. Resets at UTC midnight."}
```

Live status: `GET /api/chat/budget`.

## 2. Weekly digest (one LLM call per week)

If you only need a periodic real-estate briefing — not always-on chat —
use `reip digest`. **One Claude call generates a markdown brief**: current
mortgage rate, top regime-adjusted IRR zips, archetype context, one risk.

```bash
$ reip digest --dry-run         # print the prompt + estimated cost, no API
$ reip digest                    # write ~/.reip/digest-2026-05-11.md
$ reip digest --out brief.md     # custom path
```

Typical cost: **$0.02-0.05 per run**. At weekly cadence: **≈ $1-3/month**.

### Schedule it weekly (macOS launchd)

```xml
<!-- ~/Library/LaunchAgents/com.reip.digest.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.reip.digest</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/realestate-platform/.venv/bin/reip</string>
    <string>digest</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>1</integer>
    <key>Hour</key><integer>8</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/Users/YOU/.reip/digest.stdout.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/.reip/digest.stderr.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.reip.digest.plist
```

### Or cron (Linux / WSL)

```cron
0 8 * * 1 /home/YOU/realestate-platform/.venv/bin/reip digest
```

## 3. Per-call usage telemetry

Every chat call logs token + cost to `~/.reip/chat_usage.jsonl`. Aggregate:

```bash
$ curl http://127.0.0.1:8787/api/chat/usage?days=7
```

Returns total cost, per-model breakdown, cache-hit %, last 20 calls. If you
ever see spend creep up unexpectedly, this catches it in seconds — not in
24 hours via the Anthropic dashboard.

## Recommended setup

For most users:

1. Set `CHAT_DAILY_BUDGET_USD=2.00` (or lower) in `.env`
2. Set a hard monthly spend cap in the [Anthropic Workspace dashboard](https://console.anthropic.com/settings/limits) — $25-50/mo is plenty
3. Schedule `reip digest` weekly via launchd/cron
4. Check `/api/chat/budget` and `/api/chat/usage` periodically

Total predictable monthly cost: **≤$25-50** (95% from interactive chat,
<$5 from weekly digests).
