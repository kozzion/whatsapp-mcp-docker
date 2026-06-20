# CLAUDE.md

Guidance for working in this repo (a dockerized Python WhatsApp MCP server).

## Always check data freshness before a big query

Before answering any substantial question from the WhatsApp data (events,
"what's happening", inventories, contact lookups, anything the user will act
on), **first verify the local `messages.db` is up to date.** The DB only
reflects what has synced — it can lag the real WhatsApp state (e.g. if the
container was down, or history sync hasn't caught up).

Steps before a big query:

1. Confirm the bridge is connected:
   `curl -s http://localhost:8080/api/status` → expect `"connected": true`.
   If not, bring it up: `docker compose up -d` and wait for `connected`.
2. Check the newest message timestamp against the real current date:
   `sqlite3 "$DB" "SELECT MAX(timestamp) FROM messages;"`
   (DB path: `$PATH_DIR_DATA_WHATSAPPMCP/messages.db`).
   If the newest message is meaningfully older than "now", the data is stale —
   the sync needs to catch up before you answer.
3. If stale, note it to the user and let the live connection/history sync
   refresh before drawing conclusions; don't present stale data as current.

## Data quirks

- `chats.last_message_time` is **unreliable** (mis-set during history sync).
  For recency/"last active", always use `MAX(messages.timestamp)` grouped by
  `chat_jid`, never `chats.last_message_time`.
- Group senders are often stored as `@lid` (WhatsApp privacy IDs), which are
  **not** addressable for group operations — map to phone-number JIDs
  (`@s.whatsapp.net`) via the `contacts` table before adding/removing people.
