from typing import List, Dict, Any, Optional
from mcp.server.fastmcp import FastMCP
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    send_message as whatsapp_send_message,
    send_file as whatsapp_send_file,
    send_audio_message as whatsapp_audio_voice_message,
    download_media as whatsapp_download_media,
    create_group as whatsapp_create_group,
    create_channel as whatsapp_create_channel,
    update_group_participants as whatsapp_update_group_participants
)

# Initialize FastMCP server. host/port matter only for the streamable-http
# transport; inside a container set FASTMCP_HOST=0.0.0.0 so the port is
# reachable from the host (the default 127.0.0.1 only binds container loopback).
import os
from dataclasses import asdict, is_dataclass
mcp = FastMCP(
    "whatsapp",
    host=os.environ.get("FASTMCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("FASTMCP_PORT", "8000")),
)

def _jsonable(obj):
    """Convert dataclasses (Chat/Contact/MessageContext) to plain dicts so they
    satisfy the MCP tool output schema (mcp>=1.12 validates tool results)."""
    if obj is None:
        return {}
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, list):
        return [asdict(x) if is_dataclass(x) else x for x in obj]
    return obj

@mcp.tool()
def search_contacts(query: str) -> List[Dict[str, Any]]:
    """Search WhatsApp contacts by name or phone number.
    
    Args:
        query: Search term to match against contact names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return _jsonable(contacts)

@mcp.tool()
def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> str:
    """Get WhatsApp messages matching specified criteria with optional context.
    
    Args:
        after: Optional ISO-8601 formatted string to only return messages after this date
        before: Optional ISO-8601 formatted string to only return messages before this date
        sender_phone_number: Optional phone number to filter messages by sender
        chat_jid: Optional chat JID to filter messages by chat
        query: Optional search term to filter messages by content
        limit: Maximum number of messages to return (default 20)
        page: Page number for pagination (default 0)
        include_context: Whether to include messages before and after matches (default True)
        context_before: Number of messages to include before each match (default 1)
        context_after: Number of messages to include after each match (default 1)
    """
    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after
    )
    return messages if isinstance(messages, str) else ""

@mcp.tool()
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.
    
    Args:
        query: Optional search term to filter chats by name or JID
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        include_last_message: Whether to include the last message in each chat (default True)
        sort_by: Field to sort results by, either "last_active" or "name" (default "last_active")
    """
    chats = whatsapp_list_chats(
        query=query,
        limit=limit,
        page=page,
        include_last_message=include_last_message,
        sort_by=sort_by
    )
    return _jsonable(chats)

@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by JID.
    
    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return _jsonable(chat)

@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by sender phone number.
    
    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return _jsonable(chat)

@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    chats = whatsapp_get_contact_chats(jid, limit, page)
    return _jsonable(chats)

@mcp.tool()
def get_last_interaction(jid: str) -> str:
    """Get most recent WhatsApp message involving the contact.
    
    Args:
        jid: The JID of the contact to search for
    """
    message = whatsapp_get_last_interaction(jid)
    return message or ""

@mcp.tool()
def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> Dict[str, Any]:
    """Get context around a specific WhatsApp message.
    
    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
    """
    context = whatsapp_get_message_context(message_id, before, after)
    return _jsonable(context)

@mcp.tool()
def send_message(
    recipient: str,
    message: str
) -> Dict[str, Any]:
    """Send a WhatsApp message to a person or group. For group chats use the JID.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        message: The message text to send
    
    Returns:
        A dictionary containing success status and a status message
    """
    # Validate input
    if not recipient:
        return {
            "success": False,
            "message": "Recipient must be provided"
        }
    
    # Call the whatsapp_send_message function with the unified recipient parameter
    success, status_message = whatsapp_send_message(recipient, message)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_file(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send a file such as a picture, raw audio, video or document via WhatsApp to the specified recipient. For group messages use the JID.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the media file to send (image, video, document)
    
    Returns:
        A dictionary containing success status and a status message
    """
    
    # Call the whatsapp_send_file function
    success, status_message = whatsapp_send_file(recipient, media_path)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_audio_message(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send any audio file as a WhatsApp audio message to the specified recipient. For group messages use the JID. If it errors due to ffmpeg not being installed, use send_file instead.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the audio file to send (will be converted to Opus .ogg if it's not a .ogg file)
    
    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_audio_voice_message(recipient, media_path)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> Dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    file_path = whatsapp_download_media(message_id, chat_jid)
    
    if file_path:
        return {
            "success": True,
            "message": "Media downloaded successfully",
            "file_path": file_path
        }
    else:
        return {
            "success": False,
            "message": "Failed to download media"
        }

@mcp.tool()
def create_group(name: str, participants: Optional[List[str]] = None) -> Dict[str, Any]:
    """Create a new WhatsApp group.

    By default the group is created empty (just you as admin) and an invite link
    is returned, so you can share it rather than adding people without consent.

    Args:
        name: The name/subject of the new group
        participants: Optional list of phone numbers (country code, no +) or JIDs
                      to add immediately. Omit to create an empty group.

    Returns:
        A dictionary with success status, a message, and (on success) the group
        JID, name, invite_link, and number of participants added.
    """
    if not name:
        return {"success": False, "message": "Group name must be provided"}
    success, status_message, info = whatsapp_create_group(name, participants)
    result = {"success": success, "message": status_message}
    if info:
        result.update(info)
    return result

@mcp.tool()
def create_channel(name: str, description: str = "") -> Dict[str, Any]:
    """Create a new WhatsApp Channel (a one-way broadcast feed / newsletter).

    Args:
        name: The name of the channel
        description: Optional channel description

    Returns:
        A dictionary with success status, a message, and (on success) the
        channel id and name.
    """
    if not name:
        return {"success": False, "message": "Channel name must be provided"}
    success, status_message, info = whatsapp_create_channel(name, description)
    result = {"success": success, "message": status_message}
    if info:
        result.update(info)
    return result

@mcp.tool()
def update_group_participants(group_jid: str, participants: List[str], action: str) -> Dict[str, Any]:
    """Add, remove, promote, or demote participants in an existing WhatsApp group.

    Args:
        group_jid: The JID of the group (e.g. "123...@g.us")
        participants: List of phone numbers (country code, no +) or JIDs to change
        action: One of "add", "remove", "promote", "demote"

    Returns:
        A dictionary with success status and a status message.
    """
    if not group_jid:
        return {"success": False, "message": "Group JID must be provided"}
    if not participants:
        return {"success": False, "message": "At least one participant must be provided"}
    success, status_message, info = whatsapp_update_group_participants(group_jid, participants, action)
    result = {"success": success, "message": status_message}
    if info:
        result.update(info)
    return result

if __name__ == "__main__":
    # Start the neonize WhatsApp bridge (background thread + on-demand login
    # control API). It auto-resumes an existing session, otherwise idles until
    # POST /api/login so no QR codes are generated unprompted.
    import whatsapp_bridge
    whatsapp_bridge.start()

    # Transport is selectable via MCP_TRANSPORT (default "stdio").
    # Set MCP_TRANSPORT=streamable-http to run a persistent networked server;
    # host/port are read from FASTMCP_HOST / FASTMCP_PORT.
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)