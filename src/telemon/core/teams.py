"""Team / Guild core logic — create, join, leave, promote, XP, leveling."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.database.models.team import Team
from telemon.database.models.user import User
from telemon.logging import get_logger
from telemon.core.text import esc

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Level thresholds — XP required to reach each level
# Level N requires LEVEL_XP[N] *cumulative* XP.  max_members grows each level.
# ---------------------------------------------------------------------------

MAX_TEAM_LEVEL = 20

def xp_for_level(level: int) -> int:
    """XP required to reach a given level (cumulative)."""
    if level <= 1:
        return 0
    # Quadratic curve: 500 * (level-1)^2
    return 500 * ((level - 1) ** 2)


def max_members_for_level(level: int) -> int:
    """Max members allowed at a given level."""
    # Starts at 10, +2 per level
    return 10 + (level - 1) * 2


# XP rewards per event
TEAM_XP_REWARDS: dict[str, int] = {
    "catch": 5,
    "battle_win": 10,
    "evolve": 8,
    "trade": 3,
    "breed": 6,
    "quest_complete": 15,
    "achievement": 20,
}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

TAG_PATTERN = re.compile(r"^[A-Z0-9]{2,5}$")


def validate_tag(tag: str) -> str | None:
    """Validate and normalize a team tag.  Returns normalized tag or None."""
    tag = tag.upper().strip()
    if TAG_PATTERN.match(tag):
        return tag
    return None


def validate_name(name: str) -> str | None:
    """Validate team name.  3-32 chars, no leading/trailing space."""
    name = name.strip()
    if 3 <= len(name) <= 32:
        return name
    return None


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

async def create_team(
    session: AsyncSession,
    leader_id: int,
    name: str,
    tag: str,
) -> tuple[Team | None, str]:
    """Create a new team.  Returns (team, error_message).

    On success error_message is empty.
    """
    # Validate inputs
    clean_name = validate_name(name)
    if not clean_name:
        return None, "Team name must be 3-32 characters."

    clean_tag = validate_tag(tag)
    if not clean_tag:
        return None, "Tag must be 2-5 uppercase letters/numbers."

    # Check user exists and has no team
    user = await session.get(User, leader_id)
    if not user:
        return None, "You need to /start first."
    if user.team_id is not None:
        return None, "You are already in a team. Leave first with /team leave."

    # Check name uniqueness (case-insensitive)
    existing = await session.execute(
        select(Team).where(func.lower(Team.name) == clean_name.lower())
    )
    if existing.scalar_one_or_none():
        return None, f'A team named "{clean_name}" already exists.'

    # Check tag uniqueness
    existing = await session.execute(
        select(Team).where(Team.tag == clean_tag)
    )
    if existing.scalar_one_or_none():
        return None, f"Tag [{clean_tag}] is already taken."

    # Create
    team = Team(
        name=clean_name,
        tag=clean_tag,
        leader_id=leader_id,
        settings={"join_policy": "open"},
    )
    session.add(team)
    await session.flush()  # get team.id

    # Assign leader
    user.team_id = team.id
    user.team_role = "leader"
    await session.commit()
    await session.refresh(team)

    logger.info("Team created: [%s] %s by user %d", clean_tag, clean_name, leader_id)
    return team, ""


async def join_team(
    session: AsyncSession,
    user_id: int,
    tag: str,
) -> tuple[Team | None, str]:
    """Join an existing team by tag."""
    user = await session.get(User, user_id)
    if not user:
        return None, "You need to /start first."
    if user.team_id is not None:
        return None, "You are already in a team. Leave first with /team leave."

    clean_tag = tag.upper().strip()
    result = await session.execute(select(Team).where(Team.tag == clean_tag))
    team = result.scalar_one_or_none()
    if not team:
        return None, f"No team with tag [{clean_tag}] found."

    # Check join policy
    policy = (team.settings or {}).get("join_policy", "open")
    if policy == "invite_only":
        return None, f"[{esc(team.tag)}] {esc(team.name)} is invite-only."

    # Check capacity
    member_count = await _count_members(session, team.id)
    if member_count >= team.max_members:
        return None, f"[{esc(team.tag)}] {esc(team.name)} is full ({member_count}/{team.max_members})."

    user.team_id = team.id
    user.team_role = "member"
    await session.commit()

    logger.info("User %d joined team [%s]", user_id, team.tag)
    return team, ""


async def leave_team(
    session: AsyncSession,
    user_id: int,
) -> tuple[bool, str]:
    """Leave current team.  Leaders must transfer or disband first."""
    user = await session.get(User, user_id)
    if not user or user.team_id is None:
        return False, "You are not in a team."

    team = await session.get(Team, user.team_id)
    if not team:
        # Orphan reference — just clear
        user.team_id = None
        user.team_role = None
        await session.commit()
        return True, "Left team."

    if team.leader_id == user_id:
        return False, "You are the leader. Promote someone else first (/team promote @user) or /team disband."

    user.team_id = None
    user.team_role = None
    await session.commit()
    logger.info("User %d left team [%s]", user_id, team.tag)
    return True, f"You left [{esc(team.tag)}] {esc(team.name)}."


async def kick_member(
    session: AsyncSession,
    kicker_id: int,
    target_id: int,
) -> tuple[bool, str]:
    """Kick a member.  Only leader and officers can kick."""
    kicker = await session.get(User, kicker_id)
    if not kicker or kicker.team_id is None:
        return False, "You are not in a team."

    # Permission check
    if kicker.team_role not in ("leader", "officer"):
        return False, "Only leaders and officers can kick members."

    target = await session.get(User, target_id)
    if not target or target.team_id != kicker.team_id:
        return False, "That user is not in your team."

    if target_id == kicker_id:
        return False, "You can't kick yourself."

    team = await session.get(Team, kicker.team_id)

    # Officers can't kick other officers or leader
    if kicker.team_role == "officer" and target.team_role in ("leader", "officer"):
        return False, "Officers cannot kick other officers or the leader."

    # Leader can't be kicked
    if target.team_role == "leader":
        return False, "You cannot kick the team leader."

    target.team_id = None
    target.team_role = None
    await session.commit()

    tag = team.tag if team else "?"
    logger.info("User %d kicked %d from team [%s]", kicker_id, target_id, tag)
    return True, f"Kicked {esc(target.display_name)} from the team."


async def promote_member(
    session: AsyncSession,
    leader_id: int,
    target_id: int,
    to_role: str = "officer",
) -> tuple[bool, str]:
    """Promote a member.  Only the leader can promote."""
    leader = await session.get(User, leader_id)
    if not leader or leader.team_id is None:
        return False, "You are not in a team."
    if leader.team_role != "leader":
        return False, "Only the leader can promote members."

    target = await session.get(User, target_id)
    if not target or target.team_id != leader.team_id:
        return False, "That user is not in your team."

    if target_id == leader_id:
        return False, "You can't promote yourself."

    if to_role == "leader":
        # Transfer leadership
        team = await session.get(Team, leader.team_id)
        if team:
            team.leader_id = target_id
        target.team_role = "leader"
        leader.team_role = "officer"
        await session.commit()
        return True, f"Leadership transferred to {esc(target.display_name)}. You are now an officer."

    target.team_role = to_role
    await session.commit()
    return True, f"{esc(target.display_name)} promoted to {to_role}."


async def demote_member(
    session: AsyncSession,
    leader_id: int,
    target_id: int,
) -> tuple[bool, str]:
    """Demote an officer to member.  Only the leader can demote."""
    leader = await session.get(User, leader_id)
    if not leader or leader.team_id is None:
        return False, "You are not in a team."
    if leader.team_role != "leader":
        return False, "Only the leader can demote members."

    target = await session.get(User, target_id)
    if not target or target.team_id != leader.team_id:
        return False, "That user is not in your team."

    if target.team_role != "officer":
        return False, "That user is not an officer."

    target.team_role = "member"
    await session.commit()
    return True, f"{esc(target.display_name)} demoted to member."


async def disband_team(
    session: AsyncSession,
    leader_id: int,
) -> tuple[bool, str]:
    """Disband (delete) a team.  Only leader can do this."""
    leader = await session.get(User, leader_id)
    if not leader or leader.team_id is None:
        return False, "You are not in a team."
    if leader.team_role != "leader":
        return False, "Only the leader can disband the team."

    team = await session.get(Team, leader.team_id)
    if not team:
        leader.team_id = None
        leader.team_role = None
        await session.commit()
        return True, "Team disbanded."

    tag, name = esc(team.tag), esc(team.name)

    # Remove all members
    result = await session.execute(
        select(User).where(User.team_id == team.id)
    )
    members = list(result.scalars().all())
    for m in members:
        m.team_id = None
        m.team_role = None

    await session.delete(team)
    await session.commit()

    logger.info("Team [%s] %s disbanded by %d", tag, name, leader_id)
    return True, f"[{tag}] {name} has been disbanded."


async def set_team_description(
    session: AsyncSession,
    user_id: int,
    description: str,
) -> tuple[bool, str]:
    """Set team description.  Leader/officers only."""
    user = await session.get(User, user_id)
    if not user or user.team_id is None:
        return False, "You are not in a team."
    if user.team_role not in ("leader", "officer"):
        return False, "Only leaders and officers can change the description."

    team = await session.get(Team, user.team_id)
    if not team:
        return False, "Team not found."

    desc = description.strip()[:200]
    team.description = desc if desc else None
    await session.commit()
    return True, "Team description updated."


async def set_team_tag(
    session: AsyncSession,
    user_id: int,
    new_tag: str,
) -> tuple[bool, str]:
    """Change team tag.  Leader only.  Costs nothing for now."""
    user = await session.get(User, user_id)
    if not user or user.team_id is None:
        return False, "You are not in a team."
    if user.team_role != "leader":
        return False, "Only the leader can change the tag."

    clean_tag = validate_tag(new_tag)
    if not clean_tag:
        return False, "Tag must be 2-5 uppercase letters/numbers."

    # Check uniqueness
    existing = await session.execute(
        select(Team).where(Team.tag == clean_tag)
    )
    existing_team = existing.scalar_one_or_none()
    if existing_team and existing_team.id != user.team_id:
        return False, f"Tag [{clean_tag}] is already taken."

    team = await session.get(Team, user.team_id)
    if team:
        team.tag = clean_tag
        await session.commit()
    return True, f"Tag changed to [{clean_tag}]."


async def set_join_policy(
    session: AsyncSession,
    user_id: int,
    policy: str,
) -> tuple[bool, str]:
    """Set join policy: 'open' or 'invite_only'.  Leader only."""
    user = await session.get(User, user_id)
    if not user or user.team_id is None:
        return False, "You are not in a team."
    if user.team_role != "leader":
        return False, "Only the leader can change join policy."

    if policy not in ("open", "invite_only"):
        return False, "Policy must be 'open' or 'invite_only'."

    team = await session.get(Team, user.team_id)
    if not team:
        return False, "Team not found."

    settings = dict(team.settings or {})
    settings["join_policy"] = policy
    team.settings = settings
    await session.commit()
    nice = "Open (anyone can join)" if policy == "open" else "Invite Only"
    return True, f"Join policy set to: {nice}"


# ---------------------------------------------------------------------------
# XP & Leveling
# ---------------------------------------------------------------------------

async def add_team_xp(
    session: AsyncSession,
    team_id: int,
    event: str,
    multiplier: float = 1.0,
) -> tuple[int, int, bool]:
    """Add XP to a team for an event.

    Returns (xp_added, new_level, leveled_up).
    """
    base_xp = TEAM_XP_REWARDS.get(event, 0)
    if base_xp == 0:
        return 0, 0, False

    xp_gained = int(base_xp * multiplier)

    team = await session.get(Team, team_id)
    if not team:
        return 0, 0, False

    team.xp += xp_gained
    old_level = team.level
    leveled_up = False

    # Check for level-ups
    while team.level < MAX_TEAM_LEVEL:
        next_xp = xp_for_level(team.level + 1)
        if team.xp >= next_xp:
            team.level += 1
            team.max_members = max_members_for_level(team.level)
            leveled_up = True
        else:
            break

    await session.commit()
    return xp_gained, team.level, leveled_up


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

async def get_team_info(
    session: AsyncSession,
    team_id: int,
) -> dict[str, Any] | None:
    """Get full team info dict for display."""
    team = await session.get(Team, team_id)
    if not team:
        return None

    member_count = await _count_members(session, team_id)

    # Get leader display name
    leader = await session.get(User, team.leader_id)
    leader_name = esc(leader.display_name) if leader else "Unknown"

    # XP progress to next level
    current_xp = team.xp
    if team.level >= MAX_TEAM_LEVEL:
        next_xp = current_xp  # maxed
        xp_progress = "MAX"
    else:
        next_xp = xp_for_level(team.level + 1)
        xp_progress = f"{current_xp}/{next_xp}"

    return {
        "team": team,
        "member_count": member_count,
        "leader_name": leader_name,
        "xp_progress": xp_progress,
    }


async def get_team_members(
    session: AsyncSession,
    team_id: int,
    page: int = 1,
    per_page: int = 15,
) -> tuple[list[User], int]:
    """Get paginated team members.  Returns (members, total_count)."""
    total = await _count_members(session, team_id)

    result = await session.execute(
        select(User)
        .where(User.team_id == team_id)
        .order_by(
            # Leader first, then officers, then members
            func.array_position(
                ["leader", "officer", "member"],
                func.coalesce(User.team_role, "member"),
            ),
            User.created_at.asc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    members = list(result.scalars().all())
    return members, total


async def list_teams(
    session: AsyncSession,
    page: int = 1,
    per_page: int = 10,
) -> tuple[list[dict[str, Any]], int]:
    """Get paginated list of all teams sorted by level desc, xp desc."""
    # Total count
    count_result = await session.execute(select(func.count(Team.id)))
    total = count_result.scalar() or 0

    result = await session.execute(
        select(Team)
        .order_by(Team.level.desc(), Team.xp.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    teams = list(result.scalars().all())

    team_list = []
    for t in teams:
        mc = await _count_members(session, t.id)
        team_list.append({
            "team": t,
            "member_count": mc,
        })

    return team_list, total


async def get_user_team(
    session: AsyncSession,
    user_id: int,
) -> Team | None:
    """Get the team a user belongs to, or None."""
    user = await session.get(User, user_id)
    if not user or user.team_id is None:
        return None
    return await session.get(Team, user.team_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _count_members(session: AsyncSession, team_id: int) -> int:
    result = await session.execute(
        select(func.count(User.telegram_id)).where(User.team_id == team_id)
    )
    return result.scalar() or 0
