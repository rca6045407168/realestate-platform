# reip as an MCP server

`reip mcp` exposes the platform's 12 analytical tools over the
[Model Context Protocol](https://modelcontextprotocol.io/). Any MCP-capable
agent (Claude Code, Cursor, Continue, Zed, Claude Desktop, …) can call
them directly.

**Why this matters for cost:** the LLM calls now run on your *agent's*
quota (Claude Code Max, Cursor's plan, etc.) instead of the platform's
Anthropic API key. The platform contributes pure data analysis at $0/call.

## Setup — Claude Code

Add to `~/.claude.json` (or your project's `.mcp.json`):

```json
{
  "mcpServers": {
    "reip": {
      "command": "/Users/YOU/realestate-platform/.venv/bin/reip",
      "args": ["mcp"]
    }
  }
}
```

Restart Claude Code. The 12 tools become available — verify with:

```bash
$ claude --list-mcp-tools | grep reip
```

Then in any session:

> Use reip to stress-test a $80K Kansas City deal — $1700/mo rent, MO state.

Claude Code routes the call to `stress_test` via MCP. Verdict comes back
with full state-overlay + climate amplification + walk-away price, no
Anthropic-API spend on your platform key.

## Setup — Cursor

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "reip": {
      "command": "/Users/YOU/realestate-platform/.venv/bin/reip",
      "args": ["mcp"]
    }
  }
}
```

## Setup — Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "reip": {
      "command": "/Users/YOU/realestate-platform/.venv/bin/reip",
      "args": ["mcp"]
    }
  }
}
```

## Tools exposed

| Tool | What it does |
|---|---|
| `top_zips` | Rank US zips by regime-adjusted 5y IRR |
| `top_msas` | Rank US MSAs by Appreciation × Cashflow × Risk |
| `msa_detail` | Full breakdown for one CBSA |
| `live_listings` | Active Redfin listings (11 verified metros) |
| `underwrite` | Single-deal pro forma + DSCR + IRR + sensitivity |
| `avm_zips` | Cold/hot zips by Redfin-vs-Zillow divergence |
| `parse_remarks` | 8 alpha flags from MLS remarks (incl. auction) |
| `buy_box` | Per-zip target price/rent/rehab/ARV + climate + MSA stability |
| `stress_test` | Base/stress/worst with state + climate overlays + rate curve |
| `strategy_backtest` | 50y empirical analysis sections |
| `portfolio_resilience` | Pipeline historical-DD score (requires pipeline context) |
| `current_rates` | Today's mortgage / treasury / fed funds |

## Sanity check

```bash
# Manual stdio test — JSON-RPC over stdin/stdout
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | reip mcp

# Spawn + list tools (more realistic)
python -c "
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    p = StdioServerParameters(command='reip', args=['mcp'])
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            t = await s.list_tools()
            print(f'{len(t.tools)} tools available')
asyncio.run(main())
"
```

## Cost comparison

| Path | Anthropic spend per stress-test |
|---|---:|
| `/api/chat` (platform key) | ~$0.04 |
| `reip mcp` via Claude Code Max | **$0** (rolled into Max subscription) |

If you're a Claude Code Max subscriber, route all real-estate analysis
through MCP and the platform's monthly Anthropic spend drops to roughly
the cost of the weekly `reip digest` (~$1-3/month).
