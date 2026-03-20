"""Extract quoted message context from Telegram reply-to-message."""

from typing import Optional

from telegram import Message


def extract_reply_context(message: Message) -> Optional[str]:
    """Extract quoted message text when the user replies to a previous message."""
    reply = message.reply_to_message
    if reply is None:
        return None

    # InaccessibleMessage has no .text — guard with getattr
    text = getattr(reply, "text", None) or getattr(reply, "caption", None)
    if not text:
        return None

    # Truncate to avoid bloating the prompt
    if len(text) > 500:
        text = text[:500] + "..."

    return f'[Replying to: "{text}"]'
