"""Button ownership tracking for inline keyboard access control.

When a command creates a message with inline buttons, the sender's user ID
is recorded via ``set_owner``.  Before processing a callback, handlers call
``check_owner`` to verify that the person clicking is the original invoker.

The store is intentionally in-memory only — button ownership does not need
to survive restarts, and keeping it ephemeral avoids Redis/DB overhead for
a simple UX guard.
"""

_button_owners: dict[int, int] = {}  # message_id -> telegram_user_id
_MAX_SIZE = 2000


def set_owner(message_id: int, user_id: int) -> None:
    """Record that *user_id* owns the buttons on *message_id*."""
    if len(_button_owners) > _MAX_SIZE:
        # Evict oldest half to cap memory
        to_remove = list(_button_owners.keys())[: _MAX_SIZE // 2]
        for k in to_remove:
            del _button_owners[k]
    _button_owners[message_id] = user_id


def check_owner(message_id: int, user_id: int) -> bool:
    """Return True if *user_id* is allowed to use the buttons on *message_id*.

    Returns True when there is no owner recorded (legacy messages, DMs, etc.).
    """
    owner = _button_owners.get(message_id)
    return owner is None or owner == user_id
