# IG Researcher (Claude Code Plugin + MCP)

Headful Instagram research via MCP tools, designed for Claude Code / Claude Desktop.

## Setup

```bash
uv sync
uv run playwright install
```

Set environment variable:

```bash
export GEMINI_API_KEY=...
```

Claude Code/Claude Desktop handles chat authentication; no Anthropic API keys are required for MCP usage.

For macOS users, you can store the key securely in Keychain using the MCP tool `configure_gemini_key`.

## Usage

- Install the plugin from your private marketplace.
- Use the MCP tool `research_socials` (multi-search + dedupe) or call `search_instagram` + `fetch_and_analyze`.
