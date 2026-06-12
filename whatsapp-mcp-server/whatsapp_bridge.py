"""
WhatsApp bridge powered by neonize (whatsmeow under the hood).

Replaces the separate Go bridge: it runs the WhatsApp connection in a
background thread, persists incoming messages to the same SQLite schema the
MCP server reads, and exposes send/download helpers plus an on-demand login
control HTTP API (so the bridge never generates QR codes until asked).

Everything runs inside the MCP server process — one language, one container.
"""
import json
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from neonize.client import NewClient
from neonize.events import (
    ConnectedEv,
    MessageEv,
    PairStatusEv,
    LoggedOutEv,
    HistorySyncEv,
)
from neonize.utils import build_jid, Jid2String
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message as WAMessage

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
STORE_DIR = os.environ.get(
    "WHATSAPP_STORE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "store"),
)
os.makedirs(STORE_DIR, exist_ok=True)
MESSAGES_DB_PATH = os.path.join(STORE_DIR, "messages.db")
SESSION_DB_PATH = os.path.join(STORE_DIR, "session.db")
CONTROL_PORT = int(os.environ.get("WHATSAPP_CONTROL_PORT", "8080"))

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_client: "NewClient | None" = None
_db_lock = threading.Lock()
_conn_lock = threading.Lock()
_connect_thread: "threading.Thread | None" = None
_login_in_progress = False
_connected = False

# jid string -> display name (contacts + group subjects + pushnames)
_names: "dict[str, str]" = {}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def _init_db():
    conn = sqlite3.connect(MESSAGES_DB_PATH)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chats (
                jid TEXT PRIMARY KEY,
                name TEXT,
                last_message_time TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS contacts (
                jid TEXT PRIMARY KEY,
                name TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id TEXT,
                chat_jid TEXT,
                sender TEXT,
                content TEXT,
                timestamp TIMESTAMP,
                is_from_me BOOLEAN,
                media_type TEXT,
                filename TEXT,
                raw_message BLOB,
                PRIMARY KEY (id, chat_jid),
                FOREIGN KEY (chat_jid) REFERENCES chats(jid)
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def _ts_to_str(ts) -> str:
    """neonize Info.Timestamp -> ISO string consumed by whatsapp.py."""
    try:
        return datetime.fromtimestamp(int(ts)).isoformat(sep=" ")
    except Exception:
        return datetime.now().isoformat(sep=" ")


def _upsert_chat(jid: str, name: str, ts_str: str):
    with _db_lock:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        try:
            conn.execute(
                """INSERT INTO chats (jid, name, last_message_time)
                   VALUES (?, ?, ?)
                   ON CONFLICT(jid) DO UPDATE SET
                       last_message_time=excluded.last_message_time,
                       name=COALESCE(NULLIF(excluded.name, ''), chats.name)""",
                (jid, name or jid, ts_str),
            )
            conn.commit()
        finally:
            conn.close()


def _store_message(mid, chat_jid, sender, content, ts_str, is_from_me,
                   media_type, filename, raw_blob):
    with _db_lock:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        try:
            conn.execute(
                """INSERT INTO messages
                   (id, chat_jid, sender, content, timestamp, is_from_me,
                    media_type, filename, raw_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id, chat_jid) DO UPDATE SET
                       content=excluded.content,
                       media_type=excluded.media_type,
                       filename=excluded.filename,
                       raw_message=excluded.raw_message""",
                (mid, chat_jid, sender, content, ts_str, 1 if is_from_me else 0,
                 media_type, filename, raw_blob),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Message parsing helpers
# ---------------------------------------------------------------------------
def _extract(msg: WAMessage):
    """Return (text, media_type, filename) from a waE2E Message."""
    if msg.conversation:
        return msg.conversation, None, None
    if msg.HasField("extendedTextMessage"):
        return msg.extendedTextMessage.text, None, None
    if msg.HasField("imageMessage"):
        return msg.imageMessage.caption, "image", None
    if msg.HasField("videoMessage"):
        return msg.videoMessage.caption, "video", None
    if msg.HasField("audioMessage"):
        return "", "audio", None
    if msg.HasField("documentMessage"):
        return msg.documentMessage.caption, "document", msg.documentMessage.fileName
    if msg.HasField("stickerMessage"):
        return "", "sticker", None
    return "", None, None


# ---------------------------------------------------------------------------
# Name resolution (contacts + group subjects + pushnames)
# ---------------------------------------------------------------------------
def _best_contact_name(info) -> str:
    for attr in ("FullName", "FirstName", "PushName", "BusinessName"):
        v = getattr(info, attr, "")
        if v:
            return v
    return ""


def _group_subject(gi) -> str:
    try:
        return gi.GroupName.Name
    except Exception:
        return ""


def _resolve_name(jid_str: str) -> str:
    return _names.get(jid_str, "")


def _apply_names_to_db():
    if not _names:
        return
    with _db_lock:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        try:
            conn.executemany(
                "UPDATE chats SET name=? WHERE jid=? AND (name IS NULL OR name='' OR name=jid)",
                [(n, j) for j, n in _names.items() if n],
            )
            conn.commit()
        finally:
            conn.close()


def _load_directory():
    """Pull the address book + joined groups so chats show real names."""
    if not _client:
        return
    try:
        for c in _client.contact.get_all_contacts():
            jid = Jid2String(c.JID)
            name = _best_contact_name(c.Info)
            if name:
                _names[jid] = name
    except Exception:
        print("[bridge] contact load error:\n" + traceback.format_exc(), flush=True)
    try:
        for g in _client.get_joined_groups():
            jid = Jid2String(g.JID)
            name = _group_subject(g)
            if name:
                _names[jid] = name
    except Exception:
        print("[bridge] group load error:\n" + traceback.format_exc(), flush=True)
    _apply_names_to_db()
    # Persist the full address book so contacts are searchable even if they
    # have never sent us a message (i.e. have no chat row yet).
    with _db_lock:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        try:
            conn.executemany(
                "INSERT INTO contacts (jid, name) VALUES (?, ?) "
                "ON CONFLICT(jid) DO UPDATE SET name=excluded.name",
                [(j, n) for j, n in _names.items() if n],
            )
            conn.commit()
        finally:
            conn.close()
    print(f"[bridge] resolved {len(_names)} contact/group names", flush=True)


# ---------------------------------------------------------------------------
# History sync (backfill of messages + media from before we connected)
# ---------------------------------------------------------------------------
def _handle_history_sync(client, event: HistorySyncEv):
    try:
        convs = event.Data.conversations
        total = 0
        for conv in convs:
            chat_jid = conv.ID
            if not chat_jid:
                continue
            chat_name = _resolve_name(chat_jid) or conv.name or conv.displayName or ""
            latest_ts = None
            for hsm in conv.messages:
                wmi = hsm.message
                inner = wmi.message
                if not inner or not inner.ByteSize():
                    continue
                ts_str = _ts_to_str(wmi.messageTimestamp)
                latest_ts = ts_str
                key = wmi.key
                is_from_me = bool(key.fromMe)
                sender = key.participant or chat_jid
                text, media_type, filename = _extract(inner)
                if not text and not media_type:
                    continue
                raw_blob = inner.SerializeToString()
                # capture pushname for direct chats if we have nothing better
                if wmi.pushName and sender not in _names:
                    _names[sender] = wmi.pushName
                _store_message(key.ID, chat_jid, sender, text or "", ts_str,
                               is_from_me, media_type, filename, raw_blob)
                total += 1
            if latest_ts:
                _upsert_chat(chat_jid, chat_name, latest_ts)
        if total:
            print(f"[bridge] history sync: stored {total} messages "
                  f"across {len(convs)} conversations", flush=True)
            _apply_names_to_db()
    except Exception:
        print("[bridge] history sync error:\n" + traceback.format_exc(), flush=True)


def _handle_message(client, event: MessageEv):
    try:
        src = event.Info.MessageSource
        chat_jid = Jid2String(src.Chat)
        sender = Jid2String(src.Sender) if src.Sender.User else chat_jid
        ts_str = _ts_to_str(event.Info.Timestamp)
        text, media_type, filename = _extract(event.Message)
        pushname = event.Info.Pushname or ""
        # Prefer a resolved contact/group name; fall back to live pushname.
        chat_name = _resolve_name(chat_jid)
        if not chat_name and not src.IsGroup:
            chat_name = pushname
        if pushname and sender not in _names:
            _names[sender] = pushname
        raw_blob = event.Message.SerializeToString()

        _upsert_chat(chat_jid, chat_name, ts_str)
        _store_message(
            event.Info.ID, chat_jid, sender, text or "", ts_str,
            src.IsFromMe, media_type, filename, raw_blob,
        )
    except Exception:
        print("[bridge] error handling message:\n" + traceback.format_exc(), flush=True)


# ---------------------------------------------------------------------------
# Client setup / connection
# ---------------------------------------------------------------------------
def _build_client() -> NewClient:
    client = NewClient(SESSION_DB_PATH)

    @client.qr
    def _on_qr(_c, data_qr: bytes):
        code = data_qr.decode() if isinstance(data_qr, (bytes, bytearray)) else str(data_qr)
        print("\nScan this QR code with your WhatsApp app:", flush=True)
        print(f"QR_CODE_RAW:{code}", flush=True)

    @client.event(ConnectedEv)
    def _on_connected(_c, _e):
        global _connected, _login_in_progress
        _connected = True
        _login_in_progress = False
        print("[bridge] connected to WhatsApp", flush=True)
        # Load contacts + group names in the background once connected.
        threading.Thread(target=_load_directory, daemon=True).start()

    @client.event(HistorySyncEv)
    def _on_history(c, e):
        _handle_history_sync(c, e)

    @client.event(PairStatusEv)
    def _on_pair(_c, e: PairStatusEv):
        print(f"[bridge] paired/logged in as {e.ID.User}", flush=True)
        print("QR_CODE_RAW:PAIRED", flush=True)

    @client.event(LoggedOutEv)
    def _on_logout(_c, _e):
        global _connected
        _connected = False
        print("[bridge] logged out — re-pair required", flush=True)

    @client.event(MessageEv)
    def _on_message(c, e):
        _handle_message(c, e)

    return client


def _is_paired() -> bool:
    """True if a device session already exists (so we can auto-resume)."""
    try:
        from neonize.client import ClientFactory
        return len(ClientFactory.get_all_devices_from_db(SESSION_DB_PATH)) > 0
    except Exception:
        return os.path.exists(SESSION_DB_PATH) and os.path.getsize(SESSION_DB_PATH) > 0


def _run_connection():
    global _login_in_progress, _connected
    try:
        _client.connect()  # blocks until disconnected
    except Exception:
        print("[bridge] connection ended:\n" + traceback.format_exc(), flush=True)
    finally:
        _login_in_progress = False
        _connected = False


def start_login() -> tuple[bool, str]:
    """Begin pairing / connecting on demand."""
    global _connect_thread, _login_in_progress
    with _conn_lock:
        if _connect_thread and _connect_thread.is_alive():
            return False, "already connecting or connected"
        _login_in_progress = not _is_paired()
        _connect_thread = threading.Thread(target=_run_connection, daemon=True)
        _connect_thread.start()
    return True, "login started — watch for the QR code" if _login_in_progress \
        else "resuming existing session"


def stop_login() -> tuple[bool, str]:
    """Stop an in-progress pairing (won't drop an already-paired session)."""
    global _login_in_progress
    if _is_paired():
        return False, "already paired; refusing to disconnect a live session"
    try:
        if _client:
            _client.disconnect()
        _login_in_progress = False
        return True, "login stopped"
    except Exception as e:
        return False, f"error: {e}"


def status() -> dict:
    return {
        "paired": _is_paired(),
        "connected": _connected,
        "login_in_progress": _login_in_progress,
    }


# ---------------------------------------------------------------------------
# Sending / downloading (called by the MCP tools)
# ---------------------------------------------------------------------------
def _to_jid(recipient: str):
    if "@" in recipient:
        user, server = recipient.split("@", 1)
        return build_jid(user, server)
    return build_jid(recipient)


def _record_outgoing(resp, jid_str, content, media_type=None, filename=None):
    try:
        mid = getattr(resp, "ID", "") or ""
        ts = getattr(resp, "Timestamp", None)
        ts_str = _ts_to_str(ts) if ts else datetime.now().isoformat(sep=" ")
        _upsert_chat(jid_str, "", ts_str)
        _store_message(mid, jid_str, jid_str, content or "", ts_str, True,
                       media_type, filename, b"")
    except Exception:
        print("[bridge] error recording outgoing:\n" + traceback.format_exc(), flush=True)


def send_text(recipient: str, message: str) -> tuple[bool, str]:
    if not _client or not _connected:
        return False, "WhatsApp not connected (pair first via the login page)"
    try:
        jid = _to_jid(recipient)
        resp = _client.send_message(jid, message)
        _record_outgoing(resp, Jid2String(jid), message)
        return True, "message sent"
    except Exception as e:
        return False, f"send failed: {e}"


def send_file(recipient: str, media_path: str) -> tuple[bool, str]:
    if not _client or not _connected:
        return False, "WhatsApp not connected (pair first via the login page)"
    if not os.path.isfile(media_path):
        return False, f"file not found: {media_path}"
    try:
        jid = _to_jid(recipient)
        ext = os.path.splitext(media_path)[1].lower()
        images = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        videos = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
        audios = {".ogg", ".mp3", ".m4a", ".wav", ".aac", ".opus"}
        if ext in images:
            resp = _client.send_image(jid, media_path)
            mtype = "image"
        elif ext in videos:
            resp = _client.send_video(jid, media_path)
            mtype = "video"
        elif ext in audios:
            resp = _client.send_audio(jid, media_path)
            mtype = "audio"
        else:
            resp = _client.send_document(
                jid, media_path, filename=os.path.basename(media_path)
            )
            mtype = "document"
        _record_outgoing(resp, Jid2String(jid), "", mtype, os.path.basename(media_path))
        return True, "file sent"
    except Exception as e:
        return False, f"send failed: {e}"


def send_audio_msg(recipient: str, media_path: str) -> tuple[bool, str]:
    if not _client or not _connected:
        return False, "WhatsApp not connected (pair first via the login page)"
    if not os.path.isfile(media_path):
        return False, f"file not found: {media_path}"
    try:
        jid = _to_jid(recipient)
        # ptt=True => playable voice message; neonize transcodes via ffmpeg.
        resp = _client.send_audio(jid, media_path, ptt=True)
        _record_outgoing(resp, Jid2String(jid), "", "audio", os.path.basename(media_path))
        return True, "voice message sent"
    except Exception as e:
        return False, f"send failed: {e} (is ffmpeg installed?)"


def download_media(message_id: str, chat_jid: str) -> "str | None":
    if not _client:
        return None
    with _db_lock:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        try:
            row = conn.execute(
                "SELECT raw_message, media_type, filename FROM messages "
                "WHERE id=? AND chat_jid=?",
                (message_id, chat_jid),
            ).fetchone()
        finally:
            conn.close()
    if not row or not row[0]:
        return None
    raw_blob, media_type, filename = row
    try:
        msg = WAMessage()
        msg.ParseFromString(raw_blob)
        ext = {"image": ".jpg", "video": ".mp4", "audio": ".ogg",
               "document": "", "sticker": ".webp"}.get(media_type, "")
        chat_dir = os.path.join(STORE_DIR, chat_jid.replace(":", "_").replace("/", "_"))
        os.makedirs(chat_dir, exist_ok=True)
        name = filename or f"{message_id}{ext}"
        out_path = os.path.join(chat_dir, name)
        _client.download_any(msg, out_path)
        return out_path
    except Exception:
        print("[bridge] download error:\n" + traceback.format_exc(), flush=True)
        return None


# ---------------------------------------------------------------------------
# Control HTTP API (used by the QR login page)
# ---------------------------------------------------------------------------
class _ControlHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/status":
            self._json(200, status())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/login":
            ok, msg = start_login()
            self._json(200, {"success": ok, "message": msg})
        elif self.path == "/api/logout":
            ok, msg = stop_login()
            self._json(200, {"success": ok, "message": msg})
        else:
            self._json(404, {"error": "not found"})


def _start_control_server():
    srv = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), _ControlHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[bridge] control API on :{CONTROL_PORT} (/api/login, /api/logout, /api/status)", flush=True)


# ---------------------------------------------------------------------------
# Public bootstrap
# ---------------------------------------------------------------------------
def start():
    """Initialize DB + client + control server. Auto-resume only if paired;
    otherwise wait for an explicit /api/login (no QR codes on idle)."""
    global _client
    _init_db()
    _client = _build_client()
    _start_control_server()
    if _is_paired():
        print("[bridge] existing session found — resuming connection", flush=True)
        start_login()
    else:
        print("[bridge] no session yet — POST /api/login to pair "
              "(no QR codes generated until then)", flush=True)
