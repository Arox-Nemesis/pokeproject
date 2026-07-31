"""Text escaping for Telegram HTML messages.

The bot is configured with a global ``parse_mode=ParseMode.HTML`` (see
``telemon.bot.__init__``), so *every* outgoing message body is parsed as HTML.
Any user-controlled string interpolated into one -- a Telegram first name, an
``@username``, a Pokemon nickname, a team name or tag, a group title -- can
therefore break the parse and make the send fail outright with
``can't parse entities``.

A real example from production: a user whose first name ended in ``3!</b``
made ``/start`` unreplyable for them.

``esc`` is the single helper for that.  It is deliberately *not* applied inside
``User.display_name`` and friends, because those values are also compared,
logged and stored; escaping belongs at the boundary where the string becomes
markup, not at the model.

``quote=False`` because attribute values are never built from user input --
no keyboard ``text=`` or ``href=`` in this codebase carries a user string --
and escaping quotes would turn every apostrophe in a nickname into
``&#x27;`` inside otherwise-plain message text.
"""

from __future__ import annotations

import html


def esc(value: object) -> str:
    """Escape ``value`` for interpolation into a Telegram HTML message."""
    if value is None:
        return ""
    return html.escape(str(value), quote=False)
