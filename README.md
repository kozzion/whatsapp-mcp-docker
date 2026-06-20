# WhatsApp MCP Server

This is a Model Context Protocol (MCP) server for WhatsApp.

With this you can search and read your personal Whatsapp messages (including images, videos, documents, and audio messages), search your contacts and send messages to either individuals or groups. You can also send media files including images, videos, documents, and audio messages.

It connects to your **personal WhatsApp account** directly via the Whatsapp web multidevice API (using the [whatsmeow](https://github.com/tulir/whatsmeow) library). All your messages are stored locally in a SQLite database and only sent to an LLM (such as Claude) when the agent accesses them through tools (which you control).

Here's an example of what you can do when it's connected to Claude.

![WhatsApp MCP](./example-use.png)

> To get updates on this and other projects I work on [enter your email here](https://docs.google.com/forms/d/1rTF9wMBTN0vPfzWuQa2BjfGKdKIpTbyeKxhPMcEzgyI/preview)

> *Caution:* as with many MCP servers, the WhatsApp MCP is subject to [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/). This means that project injection could lead to private data exfiltration.

## What's different in this fork

This fork rewrites the project into a **single, dockerized Python service** (no separate Go process):

- **One container, one language.** The WhatsApp bridge is reimplemented in Python on [neonize](https://github.com/krypton-byte/neonize) (which embeds `whatsmeow`), running in a background thread inside the same process as the MCP server. No Go toolchain, no CGO, no `localhost` HTTP hop between components.
- **Dockerized.** `docker compose up -d --build` builds and runs everything. Session + message history persist in a host folder you choose (set `PATH_DIR_DATA_WHATSAPPMCP` in `.env`), bind-mounted into the container.
- **On-demand pairing.** The bridge does **not** generate QR codes on startup — it idles until you ask it to pair (`POST /api/login`), so an unattended instance never racks up WhatsApp linking attempts.
- **Browser QR page.** A small helper (`qr-server.py`) serves the rotating pairing QR at `http://localhost:8765` with explicit Generate/Stop buttons.
- **Streamable-HTTP MCP.** The MCP server runs as a persistent endpoint at `http://localhost:8001/mcp` (configurable; `stdio` still supported via `MCP_TRANSPORT`).
- **Contact/group name resolution + history sync** so chats show real names and recent history is backfilled on link.

### Quick start (this fork)

```bash
cp .env.example .env                  # then set PATH_DIR_DATA_WHATSAPPMCP to an absolute host path
docker compose up -d --build          # build + run the single container

# serve the QR pairing page (uv injects the one dependency, no global install):
uv run --with "qrcode[pil]" python qr-server.py
# open http://localhost:8765 -> Generate QR code -> scan with WhatsApp
```

Then point your MCP client at `http://localhost:8001/mcp` (Cursor: `{"url": "..."}`; Claude Desktop: add as a custom connector or via `mcp-remote`).

## Installation

### Prerequisites

- **Docker** (Docker Desktop, or Docker Engine + the Compose plugin). This is the only requirement to run the server itself — the image bundles Python, the neonize bridge, and FFmpeg.
- An MCP client — the **Anthropic Claude Desktop app**, **Cursor**, or anything that speaks streamable-HTTP MCP.
- To serve the QR pairing page: **Python 3** plus either [`uv`](https://docs.astral.sh/uv/) (recommended — `curl -LsSf https://astral.sh/uv/install.sh | sh`) or a local `pip install "qrcode[pil]"`.

> FFmpeg is installed inside the image, so audio messages are transcoded to WhatsApp's Opus voice format automatically — no host install needed.

### Steps

1. **Clone this repository**

   ```bash
   git clone https://github.com/kozzion/whatsapp-mcp-docker.git
   cd whatsapp-mcp-docker
   ```

2. **Choose where your data lives**

   Copy the example env file and set `PATH_DIR_DATA_WHATSAPPMCP` to an **absolute** host directory (use forward slashes on Windows). This folder holds your WhatsApp session, the message database, and any downloaded media:

   ```bash
   cp .env.example .env
   # edit .env, e.g. PATH_DIR_DATA_WHATSAPPMCP=/Users/you/data/whatsapp-mcp
   ```

3. **Build and start the container**

   ```bash
   docker compose up -d --build
   ```

   This starts the MCP endpoint on `http://localhost:8001/mcp` and the login control API on `http://localhost:8080` (both bound to `127.0.0.1` only). On startup the bridge **does not** generate a QR code — it waits for you to pair.

4. **Pair your WhatsApp (first run / re-authentication)**

   Start the QR page and open it in your browser:

   ```bash
   uv run --with "qrcode[pil]" python qr-server.py
   # open http://localhost:8765
   ```

   Click **Generate QR code**, then on your phone go to **WhatsApp → Settings → Linked Devices → Link a Device** and scan. The code rotates every ~20s; click **Stop** as soon as you're paired (each displayed code is a fresh linking attempt, and too many in a row makes WhatsApp say "couldn't link device, try again later").

   The session is saved to your data folder, so subsequent restarts reconnect automatically without re-scanning.

5. **Connect your MCP client**

   The server speaks **streamable-HTTP** at `http://localhost:8001/mcp`.

   - **Cursor** (`~/.cursor/mcp.json`):

     ```json
     {
       "mcpServers": {
         "whatsapp": { "url": "http://localhost:8001/mcp" }
       }
     }
     ```

   - **Claude Desktop**: add it as a custom connector pointing at `http://localhost:8001/mcp`, or bridge it with [`mcp-remote`](https://github.com/geelen/mcp-remote):

     ```json
     {
       "mcpServers": {
         "whatsapp": {
           "command": "npx",
           "args": ["mcp-remote", "http://localhost:8001/mcp"]
         }
       }
     }
     ```

   Restart your client and WhatsApp will appear as an available integration.

To view logs use `docker compose logs -f`, and to stop the server use `docker compose down`. Your data folder is on the host, so your login and history are preserved across `down`/`up` and rebuilds.

## Architecture Overview

Everything runs in **one process inside one container**:

1. **WhatsApp bridge** (`whatsapp-mcp-server/whatsapp_bridge.py`): connects to WhatsApp's web API via [neonize](https://github.com/krypton-byte/neonize) (which embeds `whatsmeow`) on a background thread, handles on-demand pairing, persists incoming messages and history-sync backfill to SQLite, resolves contact/group names, and performs sending/downloading. It also exposes a small HTTP control API (`/api/login`, `/api/logout`, `/api/status`) used by the QR page.

2. **Python MCP server** (`whatsapp-mcp-server/main.py`): a [FastMCP](https://github.com/modelcontextprotocol/python-sdk) server exposing the tools below over streamable-HTTP. It reads WhatsApp data through `whatsapp.py`, a thin query layer over the same SQLite database the bridge writes.

3. **QR page** (`qr-server.py`): a host-side helper that tails the container logs for the rotating pairing code and renders it as a PNG at `http://localhost:8765`. Run it only while pairing.

### Data Storage

- All runtime state lives in the host directory set by `PATH_DIR_DATA_WHATSAPPMCP`, bind-mounted to `/app/whatsapp-mcp-server/store` in the container.
- `session.db` — the WhatsApp device session (written on link; its presence is what lets the bridge auto-resume).
- `messages.db` — `chats`, `contacts`, and `messages` tables, indexed for search.
- `<chat_jid>/…` — subfolders holding media you've explicitly downloaded via `download_media`.

Because it's a host bind mount (not a named volume), this data survives `docker compose down`, rebuilds, and image deletion — only removing the folder itself wipes it.

### Configuration

These are set in `docker-compose.yml` / the `Dockerfile` and rarely need changing:

| Variable | Default (in container) | Purpose |
| --- | --- | --- |
| `PATH_DIR_DATA_WHATSAPPMCP` | _(required, set in `.env`)_ | Host directory bind-mounted to the store |
| `MCP_TRANSPORT` | `streamable-http` | MCP transport (`stdio` also supported) |
| `FASTMCP_HOST` / `FASTMCP_PORT` | `0.0.0.0` / `8000` | Bind address/port for the MCP endpoint |
| `WHATSAPP_STORE_DIR` | `/app/whatsapp-mcp-server/store` | Where the bridge reads/writes data |
| `WHATSAPP_CONTROL_PORT` | `8080` | Port for the login control API |

## Usage

Once connected, you can interact with your WhatsApp contacts through Claude, leveraging Claude's AI capabilities in your WhatsApp conversations.

### MCP Tools

Claude can access the following tools to interact with WhatsApp:

- **search_contacts**: Search for contacts by name or phone number
- **list_messages**: Retrieve messages with optional filters and context
- **list_chats**: List available chats with metadata
- **get_chat**: Get information about a specific chat
- **get_direct_chat_by_contact**: Find a direct chat with a specific contact
- **get_contact_chats**: List all chats involving a specific contact
- **get_last_interaction**: Get the most recent message with a contact
- **get_message_context**: Retrieve context around a specific message
- **send_message**: Send a WhatsApp message to a specified phone number or group JID
- **send_file**: Send a file (image, video, raw audio, document) to a specified recipient
- **send_audio_message**: Send an audio file as a WhatsApp voice message (transcoded to Opus via the bundled FFmpeg)
- **download_media**: Download media from a WhatsApp message and get the local file path
- **create_group**: Create a new WhatsApp group (empty by default, returning an invite link; optionally add participants)
- **create_channel**: Create a new WhatsApp Channel (a one-way broadcast feed)
- **update_group_participants**: Add, remove, promote, or demote participants in an existing group

### Media Handling Features

The MCP server supports both sending and receiving various media types:

#### Media Sending

You can send various media types to your WhatsApp contacts:

- **Images, Videos, Documents**: Use the `send_file` tool to share any supported media type.
- **Voice Messages**: Use the `send_audio_message` tool to send audio files as playable WhatsApp voice messages. FFmpeg ships in the image, so non-Opus audio (MP3, WAV, etc.) is converted automatically; you can also send raw audio with `send_file`, but it won't appear as a playable voice message.

#### Media Downloading

By default, just the metadata of the media is stored in the local database. The message will indicate that media was sent. To access this media you need to use the `download_media` tool which takes the `message_id` and `chat_jid` (which are shown when printing messages containing the media); this downloads the media into your data folder and returns the file path, which can then be opened or passed to another tool.

## Troubleshooting

- **`PATH_DIR_DATA_WHATSAPPMCP` not set**: Compose will refuse to start. Copy `.env.example` to `.env` and set an absolute path.
- **QR code not appearing**: Make sure the container is running (`docker compose ps`) and the QR page is up (`uv run --with "qrcode[pil]" python qr-server.py`). The page only shows a code after you click **Generate** — pairing is on-demand by design. The page reads the code from the container logs, so it must run on the same host as Docker.
- **"Couldn't link device, try again later"**: too many linking attempts in a short window. Wait a few minutes, then click **Generate** once and scan promptly.
- **Already logged in**: if a valid `session.db` exists, the bridge reconnects automatically on startup — no QR needed. Check `curl http://localhost:8080/api/status`.
- **Device limit reached**: WhatsApp limits linked devices. Remove one from your phone (Settings → Linked Devices).
- **No messages loading**: after initial pairing, history sync can take several minutes, especially with many chats. Watch `docker compose logs -f` for `history sync: stored N messages`.
- **WhatsApp out of sync**: stop the container (`docker compose down`), delete `session.db` and `messages.db` from your data folder, start again, and re-pair.

For additional Claude Desktop integration troubleshooting, see the [MCP documentation](https://modelcontextprotocol.io/quickstart/server#claude-for-desktop-integration-issues). The documentation includes helpful tips for checking logs and resolving common issues.
