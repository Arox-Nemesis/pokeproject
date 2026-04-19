"""Centralized message formatting utilities for consistent bot UX.

Provides compact, standardized formatters to reduce vertical space
consumption and ensure visual consistency across all handlers.
"""

from telemon.config import CURRENCY_SHORT


class Fmt:
    """Static message formatting helpers."""

    @staticmethod
    def header(text: str, emoji: str = "") -> str:
        """Bold header with optional leading emoji."""
        return f"{emoji} <b>{text}</b>" if emoji else f"<b>{text}</b>"

    @staticmethod
    def success(title: str, body: str, emoji: str = "✅") -> str:
        """Compact success message: emoji + bold title + body."""
        return f"{emoji} <b>{title}</b>\n{body}"

    @staticmethod
    def error(text: str) -> str:
        """Compact error one-liner."""
        return f"❌ {text}"

    @staticmethod
    def kv(key: str, value: object) -> str:
        """Key-value pair on a single line."""
        return f"<b>{key}:</b> {value}"

    @staticmethod
    def kv_inline(*pairs: tuple[str, object], sep: str = " | ") -> str:
        """Multiple key-value pairs joined inline.

        Example: ``Fmt.kv_inline(("Lv", 42), ("IV", "89%"))``
        → ``<b>Lv:</b> 42 | <b>IV:</b> 89%``
        """
        return sep.join(f"<b>{k}:</b> {v}" for k, v in pairs)

    @staticmethod
    def hint(text: str) -> str:
        """Italic hint text."""
        return f"<i>{text}</i>"

    @staticmethod
    def balance(amount: int) -> str:
        """Format a currency amount."""
        return f"{amount:,} {CURRENCY_SHORT}"

    @staticmethod
    def section(title: str, *lines: str) -> str:
        """A section with a bold title followed by body lines.

        Compared to the old pattern of ``<b>Title</b>\\n\\n`` followed by
        individually formatted lines, this compresses the gap to a single
        newline and joins lines tightly.
        """
        body = "\n".join(lines)
        return f"<b>{title}</b>\n{body}"
