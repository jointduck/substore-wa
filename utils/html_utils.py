"""
HTML utility for safe rendering of user-controlled text in Telegram HTML messages.

Telegram's parse_mode="HTML" will fail (and raise an exception) if the HTML is
malformed — e.g., an unclosed <tag>. Since user input can contain < > & characters,
we MUST escape them before inserting into HTML-formatted messages.

Usage:
    from utils.html_utils import safe_html

    text = f"Message from user: {safe_html(user_input)}"
"""

import html as _html


def safe_html(text: str) -> str:
    """Escape HTML special characters in user-controlled text.

    Escapes: & → &amp;, < → &lt;, > → &gt;

    Does NOT escape quotes (" → &quot;) because Telegram HTML
    doesn't use attribute values in message text.
    """
    if not text:
        return text or ""
    return _html.escape(str(text), quote=False)


def safe_code(text: str) -> str:
    """Escape text for use inside <code>...</code> tags.

    Telegram's <code> tag still parses HTML entities, so we still need
    to escape < and &. But since the text is in a code block, we don't
    need to worry about bold/italic tags.
    """
    return safe_html(text)
