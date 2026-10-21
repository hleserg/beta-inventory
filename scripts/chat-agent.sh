#!/bin/sh
# Wakes an agent for the site's chat (/chat) when the owner wrote something no agent has answered.
# Run by cron on any machine in the LAN with an agent CLI logged in, e.g. every 15 minutes:
#   */15 * * * * INVENTORY_URL=http://doctor.local:8000 /path/to/scripts/chat-agent.sh >> "$HOME/chat-agent.log" 2>&1
# Nothing waits → one tiny request and exit: no agent run, no tokens.
# CHAT_AGENT replaces the agent command; it gets the prompt as its last argument, e.g.
#   CHAT_AGENT='codex exec' (with the inventory MCP in codex's own config).
set -eu
BASE=${INVENTORY_URL:-http://localhost:8000}
BASE=${BASE%/}
n=$(curl -fsS --max-time 10 "$BASE/chat/new" | tr -cd 0-9) || exit 0  # the site is down: next time
[ "${n:-0}" -gt 0 ] || exit 0

LOCK=${TMPDIR:-/tmp}/inventory-chat-agent.lock  # one round at a time; a lock left by a killed round goes after an hour
find "$LOCK" -maxdepth 0 -mmin +60 -exec rmdir {} \; 2>/dev/null || true
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK"' EXIT INT TERM

PROMPT="Ты агент домашнего склада. В чате на сайте ждут сообщения владельца ($n). Вызови chat_inbox с agent=\"${CHAT_AGENT_NAME:-агент по чату}\", сделай по каждому сообщению, что в нём просят, как говорит skill из ответа, и ответь одним chat_reply с done. Только инструменты MCP inventory."
echo "$(date '+%F %T') $n waiting"
if [ -n "${CHAT_AGENT:-}" ]; then
  $CHAT_AGENT "$PROMPT"
else
  claude -p "$PROMPT" --mcp-config "{\"mcpServers\":{\"inventory\":{\"type\":\"http\",\"url\":\"$BASE/mcp\"}}}" \
    --strict-mcp-config --allowedTools mcp__inventory < /dev/null
fi
