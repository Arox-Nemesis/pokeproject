"""Team / Guild command handlers."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.teams import (
    add_team_xp,
    create_team,
    demote_member,
    disband_team,
    get_team_info,
    get_team_members,
    get_user_team,
    join_team,
    kick_member,
    leave_team,
    list_teams,
    max_members_for_level,
    promote_member,
    set_join_policy,
    set_team_description,
    set_team_tag,
    xp_for_level,
    MAX_TEAM_LEVEL,
)
from telemon.database.models import User
from telemon.logging import get_logger
from telemon.core.text import esc

router = Router(name="team")
logger = get_logger(__name__)

MEMBERS_PER_PAGE = 15
TEAMS_PER_PAGE = 10


# ---------------------------------------------------------------------------
# Role emoji helper
# ---------------------------------------------------------------------------

ROLE_EMOJI = {
    "leader": "👑",
    "officer": "⭐",
    "member": "👤",
}


def _role_display(role: str | None) -> str:
    r = role or "member"
    return f"{ROLE_EMOJI.get(r, '👤')} {r.capitalize()}"


# ---------------------------------------------------------------------------
# XP bar helper
# ---------------------------------------------------------------------------

def _xp_bar(current: int, target: int, width: int = 10) -> str:
    if target <= 0:
        return "█" * width
    filled = min(width, int(current / target * width))
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# /team — main router
# ---------------------------------------------------------------------------

@router.message(Command("team", "guild", "t"))
async def cmd_team(message: Message, session: AsyncSession, user: User) -> None:
    """Route /team subcommands."""
    args = (message.text or "").split(maxsplit=2)
    # /team  → show own team info
    if len(args) < 2:
        await _show_team_info(message, session, user)
        return

    sub = args[1].lower()

    if sub == "create":
        await _team_create(message, session, user, args)
    elif sub == "join":
        await _team_join(message, session, user, args)
    elif sub == "leave":
        await _team_leave(message, session, user)
    elif sub == "kick":
        await _team_kick(message, session, user)
    elif sub == "promote":
        await _team_promote(message, session, user)
    elif sub == "demote":
        await _team_demote(message, session, user)
    elif sub == "transfer":
        await _team_transfer(message, session, user)
    elif sub == "disband":
        await _team_disband(message, session, user)
    elif sub == "members":
        await _team_members(message, session, user)
    elif sub == "list":
        await _team_list(message, session)
    elif sub == "tag":
        await _team_set_tag(message, session, user, args)
    elif sub in ("desc", "description"):
        await _team_set_desc(message, session, user, args)
    elif sub == "policy":
        await _team_policy(message, session, user, args)
    elif sub == "info":
        # /team info [tag] — view another team
        if len(args) > 2:
            await _show_team_by_tag(message, session, args[2])
        else:
            await _show_team_info(message, session, user)
    else:
        await message.answer(
            "<b>Team Commands</b>\n\n"
            "/team — View your team\n"
            "/team create [name] [tag] — Create a team\n"
            "/team join [tag] — Join a team\n"
            "/team leave — Leave your team\n"
            "/team members — View members\n"
            "/team list — Browse all teams\n"
            "/team info [tag] — View a team\n"
            "/team kick — Kick a member (reply)\n"
            "/team promote — Promote to officer (reply)\n"
            "/team demote — Demote an officer (reply)\n"
            "/team transfer — Transfer leadership (reply)\n"
            "/team disband — Delete your team\n"
            "/team tag [new] — Change tag\n"
            "/team desc [text] — Set description\n"
            "/team policy [open/invite_only]"
        )


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

async def _show_team_info(message: Message, session: AsyncSession, user: User) -> None:
    """Show the user's own team info."""
    if user.team_id is None:
        await message.answer(
            "You are not in a team.\n\n"
            "Create one: /team create [name] [tag]\n"
            "Join one: /team join [tag]\n"
            "Browse: /team list"
        )
        return

    info = await get_team_info(session, user.team_id)
    if not info:
        await message.answer("Team not found.")
        return

    team = info["team"]
    policy = (team.settings or {}).get("join_policy", "open")
    policy_display = "Open" if policy == "open" else "Invite Only"

    if team.level >= MAX_TEAM_LEVEL:
        xp_line = f"XP: {team.xp:,} (MAX LEVEL)"
        bar = _xp_bar(1, 1)
    else:
        next_xp = xp_for_level(team.level + 1)
        xp_line = f"XP: {team.xp:,} / {next_xp:,}"
        bar = _xp_bar(team.xp, next_xp)

    lines = [
        f"<b>[{esc(team.tag)}] {esc(team.name)}</b>",
        "",
        f"Level: {team.level} / {MAX_TEAM_LEVEL}",
        f"{xp_line}",
        f"[{bar}]",
        "",
        f"Leader: {info['leader_name']}",
        f"Members: {info['member_count']} / {team.max_members}",
        f"Join Policy: {policy_display}",
    ]

    if team.description:
        lines.insert(1, f"<i>{esc(team.description)}</i>")

    lines.append(f"\nYour role: {_role_display(user.team_role)}")

    await message.answer("\n".join(lines))


async def _show_team_by_tag(message: Message, session: AsyncSession, tag: str) -> None:
    """Show another team's info by tag."""
    from sqlalchemy import select
    from telemon.database.models.team import Team

    clean = tag.upper().strip()
    result = await session.execute(select(Team).where(Team.tag == clean))
    team = result.scalar_one_or_none()
    if not team:
        await message.answer(f"No team with tag [{clean}] found.")
        return

    info = await get_team_info(session, team.id)
    if not info:
        await message.answer("Team not found.")
        return

    t = info["team"]
    policy = (t.settings or {}).get("join_policy", "open")
    policy_display = "Open" if policy == "open" else "Invite Only"

    if t.level >= MAX_TEAM_LEVEL:
        xp_line = f"XP: {t.xp:,} (MAX LEVEL)"
        bar = _xp_bar(1, 1)
    else:
        next_xp = xp_for_level(t.level + 1)
        xp_line = f"XP: {t.xp:,} / {next_xp:,}"
        bar = _xp_bar(t.xp, next_xp)

    lines = [
        f"<b>[{esc(t.tag)}] {esc(t.name)}</b>",
        "",
        f"Level: {t.level} / {MAX_TEAM_LEVEL}",
        f"{xp_line}",
        f"[{bar}]",
        "",
        f"Leader: {info['leader_name']}",
        f"Members: {info['member_count']} / {t.max_members}",
        f"Join Policy: {policy_display}",
    ]

    if t.description:
        lines.insert(1, f"<i>{esc(t.description)}</i>")

    await message.answer("\n".join(lines))


async def _team_create(
    message: Message, session: AsyncSession, user: User, args: list[str]
) -> None:
    """Handle /team create [name] [tag]."""
    # Parse: /team create My Cool Team COOL
    raw = (message.text or "").split(maxsplit=2)
    if len(raw) < 3:
        await message.answer(
            "Usage: /team create [name] [tag]\n\n"
            "Example: /team create Mystic Warriors MW\n"
            "Name: 3-32 chars | Tag: 2-5 uppercase letters/numbers"
        )
        return

    rest = raw[2].strip()
    # Tag is the last word
    parts = rest.rsplit(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Please provide both a name and a tag.\n"
            "Example: /team create Mystic Warriors MW"
        )
        return

    name, tag = parts[0], parts[1]

    team, error = await create_team(session, user.telegram_id, name, tag)
    if error:
        await message.answer(f"Could not create team: {error}")
        return

    # Refresh user to get updated team_id
    await session.refresh(user)

    await message.answer(
        f"Team <b>[{esc(team.tag)}] {esc(team.name)}</b> created!\n\n"
        f"You are the leader. Others can join with:\n"
        f"<code>/team join {esc(team.tag)}</code>"
    )


async def _team_join(
    message: Message, session: AsyncSession, user: User, args: list[str]
) -> None:
    """Handle /team join [tag]."""
    if len(args) < 3:
        await message.answer("Usage: /team join [tag]\nExample: /team join MW")
        return

    tag = args[2]
    team, error = await join_team(session, user.telegram_id, tag)
    if error:
        await message.answer(error)
        return

    await session.refresh(user)
    await message.answer(
        f"You joined <b>[{esc(team.tag)}] {esc(team.name)}</b>!\n"
        f"Welcome to the team."
    )


async def _team_leave(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /team leave."""
    success, msg = await leave_team(session, user.telegram_id)
    await session.refresh(user)
    await message.answer(msg)


async def _team_kick(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /team kick (reply to user's message)."""
    target_id = _get_reply_user_id(message)
    if not target_id:
        await message.answer("Reply to a team member's message to kick them.")
        return

    success, msg = await kick_member(session, user.telegram_id, target_id)
    await message.answer(msg)


async def _team_promote(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /team promote (reply to user's message)."""
    target_id = _get_reply_user_id(message)
    if not target_id:
        await message.answer("Reply to a team member's message to promote them.")
        return

    success, msg = await promote_member(session, user.telegram_id, target_id, "officer")
    await message.answer(msg)


async def _team_demote(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /team demote (reply to user's message)."""
    target_id = _get_reply_user_id(message)
    if not target_id:
        await message.answer("Reply to an officer's message to demote them.")
        return

    success, msg = await demote_member(session, user.telegram_id, target_id)
    await message.answer(msg)


async def _team_transfer(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /team transfer (reply to user's message)."""
    target_id = _get_reply_user_id(message)
    if not target_id:
        await message.answer("Reply to a team member's message to transfer leadership.")
        return

    success, msg = await promote_member(session, user.telegram_id, target_id, "leader")
    await session.refresh(user)
    await message.answer(msg)


async def _team_disband(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /team disband."""
    success, msg = await disband_team(session, user.telegram_id)
    await session.refresh(user)
    await message.answer(msg)


async def _team_set_tag(
    message: Message, session: AsyncSession, user: User, args: list[str]
) -> None:
    """Handle /team tag [new_tag]."""
    if len(args) < 3:
        await message.answer("Usage: /team tag [new_tag]\nExample: /team tag COOL")
        return

    success, msg = await set_team_tag(session, user.telegram_id, args[2])
    await message.answer(msg)


async def _team_set_desc(
    message: Message, session: AsyncSession, user: User, args: list[str]
) -> None:
    """Handle /team desc [text]."""
    raw = (message.text or "").split(maxsplit=2)
    if len(raw) < 3:
        await message.answer("Usage: /team desc [description text]\nMax 200 characters.")
        return

    # raw[2] is everything after "/team desc"
    desc_text = raw[2].strip()
    # If the user wrote "/team description ...", strip the subcommand word
    if desc_text.lower().startswith("description "):
        desc_text = desc_text[len("description "):].strip()

    success, msg = await set_team_description(session, user.telegram_id, desc_text)
    await message.answer(msg)


async def _team_policy(
    message: Message, session: AsyncSession, user: User, args: list[str]
) -> None:
    """Handle /team policy [open/invite_only]."""
    if len(args) < 3:
        await message.answer(
            "Usage: /team policy [open / invite_only]\n\n"
            "open — Anyone can join\n"
            "invite_only — Only via invite"
        )
        return

    policy = args[2].lower().strip()
    if policy in ("invite", "closed", "private"):
        policy = "invite_only"

    success, msg = await set_join_policy(session, user.telegram_id, policy)
    await message.answer(msg)


# ---------------------------------------------------------------------------
# /team members — paginated
# ---------------------------------------------------------------------------

async def _team_members(message: Message, session: AsyncSession, user: User) -> None:
    """Show team members page 1."""
    if user.team_id is None:
        await message.answer("You are not in a team.")
        return

    text, kb = await _build_members_page(session, user.team_id, 1)
    sent = await message.answer(text, reply_markup=kb.as_markup() if kb else None)

    if kb:
        from telemon.bot.handlers._button_owner import set_owner
        set_owner(sent.message_id, user.telegram_id)


async def _build_members_page(
    session: AsyncSession, team_id: int, page: int
) -> tuple[str, InlineKeyboardBuilder | None]:
    """Build a members page."""
    from telemon.database.models.team import Team

    team = await session.get(Team, team_id)
    if not team:
        return "Team not found.", None

    members, total = await get_team_members(session, team_id, page, MEMBERS_PER_PAGE)
    total_pages = max(1, (total + MEMBERS_PER_PAGE - 1) // MEMBERS_PER_PAGE)
    page = max(1, min(page, total_pages))

    lines = [f"<b>[{esc(team.tag)}] {esc(team.name)} — Members</b>  ({page}/{total_pages})\n"]

    for m in members:
        emoji = ROLE_EMOJI.get(m.team_role or "member", "👤")
        lines.append(f"{emoji} {esc(m.display_name)}")

    lines.append(f"\nTotal: {total} / {team.max_members}")

    # Navigation
    builder = InlineKeyboardBuilder()
    if page > 1:
        builder.button(text="◀️ Prev", callback_data=f"tm:{team_id}:{page - 1}")
    if page < total_pages:
        builder.button(text="Next ▶️", callback_data=f"tm:{team_id}:{page + 1}")

    return "\n".join(lines), builder if (total_pages > 1) else None


@router.callback_query(F.data.startswith("tm:"))
async def callback_team_members(callback: CallbackQuery, session: AsyncSession) -> None:
    """Handle team members pagination."""
    from telemon.bot.handlers._button_owner import check_owner
    if not check_owner(callback.message.message_id, callback.from_user.id):
        await callback.answer("These buttons aren't for you!", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    try:
        team_id = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    text, kb = await _build_members_page(session, team_id, page)
    await callback.message.edit_text(text, reply_markup=kb.as_markup() if kb else None)
    await callback.answer()


# ---------------------------------------------------------------------------
# /team list — paginated list of all teams
# ---------------------------------------------------------------------------

async def _team_list(message: Message, session: AsyncSession) -> None:
    """Show all teams page 1."""
    text, kb = await _build_team_list_page(session, 1)
    sent = await message.answer(text, reply_markup=kb.as_markup() if kb else None)

    if kb:
        from telemon.bot.handlers._button_owner import set_owner
        set_owner(sent.message_id, message.from_user.id)


async def _build_team_list_page(
    session: AsyncSession, page: int
) -> tuple[str, InlineKeyboardBuilder | None]:
    """Build a page of the team list."""
    teams, total = await list_teams(session, page, TEAMS_PER_PAGE)
    total_pages = max(1, (total + TEAMS_PER_PAGE - 1) // TEAMS_PER_PAGE)
    page = max(1, min(page, total_pages))

    if total == 0:
        return (
            "<b>Teams</b>\n\nNo teams exist yet.\n"
            "Create one with /team create [name] [tag]",
            None,
        )

    lines = [f"<b>Teams</b>  ({page}/{total_pages})\n"]

    rank_offset = (page - 1) * TEAMS_PER_PAGE
    for i, entry in enumerate(teams, start=rank_offset + 1):
        t = entry["team"]
        mc = entry["member_count"]
        lines.append(
            f"{i}. <b>[{esc(t.tag)}]</b> {esc(t.name)}  "
            f"Lv{t.level}  ({mc}/{t.max_members})"
        )

    lines.append(f"\n<i>Join with /team join [tag]</i>")

    builder = InlineKeyboardBuilder()
    if page > 1:
        builder.button(text="◀️ Prev", callback_data=f"tl:{page - 1}")
    if page < total_pages:
        builder.button(text="Next ▶️", callback_data=f"tl:{page + 1}")

    return "\n".join(lines), builder if (total_pages > 1) else None


@router.callback_query(F.data.startswith("tl:"))
async def callback_team_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Handle team list pagination."""
    from telemon.bot.handlers._button_owner import check_owner
    if not check_owner(callback.message.message_id, callback.from_user.id):
        await callback.answer("These buttons aren't for you!", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 2:
        await callback.answer()
        return

    try:
        page = int(parts[1])
    except ValueError:
        await callback.answer()
        return

    text, kb = await _build_team_list_page(session, page)
    await callback.message.edit_text(text, reply_markup=kb.as_markup() if kb else None)
    await callback.answer()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_reply_user_id(message: Message) -> int | None:
    """Get the Telegram user ID from a replied-to message."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None
