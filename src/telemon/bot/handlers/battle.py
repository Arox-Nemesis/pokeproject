"""Battle-related handlers for PvP duels and PvE wild/NPC battles."""

import random
from dataclasses import asdict

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import CURRENCY_NAME, CURRENCY_SHORT
from telemon.core.constants import MAX_LEVEL
from telemon.core.battle import (
    get_active_battle,
    create_battle,
    start_battle,
    execute_move,
    forfeit_battle,
    cancel_battle,
    MoveData,
    NPC_TRAINERS,
    PveParticipant,
    build_pve_participant_from_pokemon,
    build_pve_participant_from_pokemon_db,
    build_pve_participant_from_species,
    pve_calculate_damage,
)
# Mega evolution is now handled via /mega command (permanent species swap),
# no longer applied in-battle.
from telemon.core.leveling import (
    add_xp_to_pokemon,
    apply_xp_boost,
    calculate_wild_battle_xp,
    calculate_npc_battle_xp,
    format_xp_message,
    xp_for_next_level,
)
from telemon.database.models import Pokemon, PokemonSpecies, User
from telemon.database.models.battle import Battle, BattleStatus
from telemon.logging import get_logger

router = Router(name="battle")
logger = get_logger(__name__)

# In-memory PvE battle storage: user_id -> battle state dict
_pve_battles: dict[int, dict] = {}


def format_hp_bar(current: int, maximum: int, length: int = 10) -> str:
    """Create a visual HP bar."""
    filled = int((current / maximum) * length) if maximum > 0 else 0
    empty = length - filled
    
    if current / maximum > 0.5:
        bar_char = ""
    elif current / maximum > 0.2:
        bar_char = ""
    else:
        bar_char = ""
    
    return f"[{'█' * filled}{'░' * empty}] {current}/{maximum}"


async def format_battle_status(session: AsyncSession, battle: Battle) -> str:
    """Format the current battle status."""
    # Get players
    p1_result = await session.execute(
        select(User).where(User.telegram_id == battle.player1_id)
    )
    p1 = p1_result.scalar_one()
    
    p2_result = await session.execute(
        select(User).where(User.telegram_id == battle.player2_id)
    )
    p2 = p2_result.scalar_one()
    
    # Get Pokemon
    p1_poke_result = await session.execute(
        select(Pokemon).where(Pokemon.id == battle.player1_team[0])
    )
    p1_poke = p1_poke_result.scalar_one()
    
    p2_poke_result = await session.execute(
        select(Pokemon).where(Pokemon.id == battle.player2_team[0])
    )
    p2_poke = p2_poke_result.scalar_one()
    
    p1_hp = battle.battle_state.get("p1_hp", 0)
    p1_max = battle.battle_state.get("p1_max_hp", 1)
    p2_hp = battle.battle_state.get("p2_hp", 0)
    p2_max = battle.battle_state.get("p2_max_hp", 1)
    
    p1_name = p1.username or f"Player {battle.player1_id}"
    p2_name = p2.username or f"Player {battle.player2_id}"
    
    whose_turn_name = p1_name if battle.whose_turn == battle.player1_id else p2_name
    
    lines = [
        f"<b>Battle - Turn {battle.current_turn}</b>\n",
        f"<b>{p1_name}</b>",
        f"{p1_poke.species.name} Lv.{p1_poke.level}",
        f"HP: {format_hp_bar(p1_hp, p1_max)}",
        "",
        f"<b>{p2_name}</b>",
        f"{p2_poke.species.name} Lv.{p2_poke.level}",
        f"HP: {format_hp_bar(p2_hp, p2_max)}",
        "",
        f"<b>{whose_turn_name}'s turn!</b>",
    ]
    
    return "\n".join(lines)


def build_move_keyboard(battle: Battle, user_id: int) -> InlineKeyboardBuilder:
    """Build move selection keyboard."""
    builder = InlineKeyboardBuilder()
    
    is_p1 = user_id == battle.player1_id
    moves = battle.battle_state.get("p1_moves" if is_p1 else "p2_moves", [])
    
    for i, move in enumerate(moves):
        # Show move name and type
        move_text = f"{move['name']} ({move['type'].title()})"
        builder.button(text=move_text, callback_data=f"battle:move:{battle.id}:{i}")
    
    builder.button(text="Forfeit", callback_data=f"battle:forfeit:{battle.id}", style="danger")
    builder.adjust(2)
    
    return builder


@router.message(Command("duel", "battle"))
async def cmd_duel(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /duel command to challenge another user, or /battle wild|npc."""
    text = message.text or ""
    args = text.split()
    
    if len(args) >= 2:
        subcommand = args[1].lower()
        if subcommand == "wild":
            await cmd_battle_wild(message, session, user)
            return
        elif subcommand == "npc":
            await cmd_battle_npc(message, session, user, args)
            return
    
    if len(args) < 2:
        await message.answer(
            "<b>Battle System</b>\n\n"
            "Challenge trainers or fight wild Pokemon!\n\n"
            "<b>PvP:</b>\n"
            "/duel @username - Challenge by username\n"
            "/duel [user_id] - Challenge by ID\n\n"
            "<b>PvE:</b>\n"
            "/battle wild - Fight a random wild Pokemon\n"
            "/battle npc - List NPC trainers\n"
            "/battle npc [name] - Fight a gym leader\n\n"
            "<b>During Battle:</b>\n"
            "Select moves using inline buttons\n"
            "Type effectiveness and stats matter!\n\n"
            f"<i>Rewards: XP and {CURRENCY_NAME} for winning</i>"
        )
        return
    
    # Check if user has an active battle
    active = await get_active_battle(session, user.telegram_id)
    if active:
        if active.status == BattleStatus.ACTIVE:
            await message.answer(
                " You're already in a battle!\n"
                "Use the move buttons or /forfeit to end it."
            )
        else:
            await message.answer(
                " You have a pending challenge!\n"
                "Wait for response or /forfeit to cancel."
            )
        return
    
    # Check if user has a selected Pokemon
    if not user.selected_pokemon_id:
        # Get their first Pokemon
        result = await session.execute(
            select(Pokemon)
            .where(Pokemon.owner_id == user.telegram_id)
            .order_by(Pokemon.caught_at.asc())
            .limit(1)
        )
        first_poke = result.scalar_one_or_none()
        
        if not first_poke:
            await message.answer(
                " You don't have any Pokemon!\n"
                "Catch some first before battling."
            )
            return
        
        user.selected_pokemon_id = str(first_poke.id)
        await session.commit()
    
    # Parse target
    target_arg = args[1]
    target_user = None
    
    if target_arg.startswith("@"):
        # By username
        username = target_arg[1:]
        result = await session.execute(
            select(User).where(User.username.ilike(username))
        )
        target_user = result.scalar_one_or_none()
        
        if not target_user:
            await message.answer(f" User @{username} not found.")
            return
    elif target_arg.isdigit():
        # By ID
        result = await session.execute(
            select(User).where(User.telegram_id == int(target_arg))
        )
        target_user = result.scalar_one_or_none()
        
        if not target_user:
            await message.answer(f" User {target_arg} not found.")
            return
    else:
        await message.answer(" Please provide a valid @username or user ID.")
        return
    
    # Can't battle yourself
    if target_user.telegram_id == user.telegram_id:
        await message.answer(" You can't battle yourself!")
        return
    
    # Check if target is in a battle
    target_battle = await get_active_battle(session, target_user.telegram_id)
    if target_battle:
        await message.answer(
            f" @{target_user.username or target_user.telegram_id} is already in a battle!"
        )
        return
    
    # Get challenger's Pokemon
    challenger_poke_result = await session.execute(
        select(Pokemon).where(Pokemon.id == user.selected_pokemon_id)
    )
    challenger_poke = challenger_poke_result.scalar_one_or_none()
    
    if not challenger_poke:
        await message.answer(" Your selected Pokemon was not found!")
        return
    
    # Create battle challenge
    battle = await create_battle(
        session,
        challenger_id=user.telegram_id,
        opponent_id=target_user.telegram_id,
        challenger_pokemon_id=user.selected_pokemon_id,
        chat_id=message.chat.id,
    )
    
    logger.info(
        "Battle challenge created",
        challenger_id=user.telegram_id,
        opponent_id=target_user.telegram_id,
        battle_id=str(battle.id),
    )
    
    # Build accept/decline keyboard
    builder = InlineKeyboardBuilder()
    builder.button(text="Accept", callback_data=f"battle:accept:{battle.id}", style="success")
    builder.button(text="Decline", callback_data=f"battle:decline:{battle.id}", style="danger")
    builder.adjust(2)
    
    target_name = target_user.username or f"User {target_user.telegram_id}"
    challenger_name = user.username or f"User {user.telegram_id}"
    
    await message.answer(
        f"<b>Battle Challenge!</b>\n\n"
        f"@{challenger_name} challenges @{target_name}!\n\n"
        f"<b>{challenger_name}'s Pokemon:</b>\n"
        f"{challenger_poke.species.name} Lv.{challenger_poke.level}\n\n"
        f"@{target_name}, select your Pokemon and accept!",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("battle:accept:"))
async def callback_accept_battle(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle battle acceptance."""
    if not callback.data:
        return
    
    battle_id = callback.data.split(":")[2]
    
    result = await session.execute(
        select(Battle).where(Battle.id == battle_id)
    )
    battle = result.scalar_one_or_none()
    
    if not battle:
        await callback.answer("Battle not found!", show_alert=True)
        return
    
    if battle.player2_id != user.telegram_id:
        await callback.answer("This challenge is not for you!", show_alert=True)
        return
    
    if battle.status != BattleStatus.PENDING:
        await callback.answer("This battle is no longer available!", show_alert=True)
        return
    
    # Check if defender has a Pokemon
    if not user.selected_pokemon_id:
        # Get their first Pokemon
        poke_result = await session.execute(
            select(Pokemon)
            .where(Pokemon.owner_id == user.telegram_id)
            .order_by(Pokemon.caught_at.asc())
            .limit(1)
        )
        first_poke = poke_result.scalar_one_or_none()
        
        if not first_poke:
            await callback.answer("You don't have any Pokemon!", show_alert=True)
            return
        
        user.selected_pokemon_id = str(first_poke.id)
        await session.commit()
    
    # Start the battle
    battle_info = await start_battle(session, battle, user.selected_pokemon_id)
    
    logger.info(
        "Battle started",
        battle_id=str(battle.id),
        p1_pokemon=battle_info["p1_pokemon"].species.name,
        p2_pokemon=battle_info["p2_pokemon"].species.name,
    )
    
    # Get the player whose turn it is
    first_player_id = battle_info["first_turn"]
    
    # Format battle status
    status = await format_battle_status(session, battle)
    
    # Build move keyboard for first player
    builder = build_move_keyboard(battle, first_player_id)
    
    await callback.message.edit_text(
        f"<b>Battle Started!</b>\n\n"
        f"{battle_info['p1_pokemon'].species.name} vs {battle_info['p2_pokemon'].species.name}\n\n"
        f"{status}",
        reply_markup=builder.as_markup(),
    )
    await callback.answer("Battle started!")


@router.callback_query(lambda c: c.data and c.data.startswith("battle:decline:"))
async def callback_decline_battle(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle battle decline."""
    if not callback.data:
        return
    
    battle_id = callback.data.split(":")[2]
    
    result = await session.execute(
        select(Battle).where(Battle.id == battle_id)
    )
    battle = result.scalar_one_or_none()
    
    if not battle:
        await callback.answer("Battle not found!", show_alert=True)
        return
    
    if battle.player2_id != user.telegram_id:
        await callback.answer("This challenge is not for you!", show_alert=True)
        return
    
    if battle.status != BattleStatus.PENDING:
        await callback.answer("This battle is no longer available!", show_alert=True)
        return
    
    await cancel_battle(session, battle)
    
    await callback.message.edit_text(" Battle declined.")
    await callback.answer("Battle declined!")


@router.callback_query(lambda c: c.data and c.data.startswith("battle:move:"))
async def callback_execute_move(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle move selection."""
    if not callback.data:
        return
    
    parts = callback.data.split(":")
    battle_id = parts[2]
    move_index = int(parts[3])
    
    result = await session.execute(
        select(Battle).where(Battle.id == battle_id)
    )
    battle = result.scalar_one_or_none()
    
    if not battle:
        await callback.answer("Battle not found!", show_alert=True)
        return
    
    if battle.status != BattleStatus.ACTIVE:
        await callback.answer("This battle has ended!", show_alert=True)
        return
    
    # Check if it's the user's turn
    if battle.whose_turn != user.telegram_id:
        await callback.answer("It's not your turn!", show_alert=True)
        return
    
    # Execute the move
    move_result = await execute_move(session, battle, user.telegram_id, move_index)
    
    # Build message
    lines = [move_result["message"]]
    
    if move_result["damage"] > 0:
        lines.append(f"Dealt <b>{move_result['damage']}</b> damage!")
    
    if move_result["battle_ended"]:
        # Battle is over
        winner_result = await session.execute(
            select(User).where(User.telegram_id == move_result["winner_id"])
        )
        winner = winner_result.scalar_one()
        
        # Award rewards
        winner.balance += move_result["winner_coins"]
        
        # Add XP to winner's Pokemon using leveling system
        winner_poke_id = (battle.player1_team[0] if move_result["winner_id"] == battle.player1_id 
                         else battle.player2_team[0])
        xp_added, levels_gained, learned_moves = await add_xp_to_pokemon(
            session, str(winner_poke_id), move_result["winner_xp"]
        )
        
        if levels_gained:
            winner_poke_result = await session.execute(
                select(Pokemon).where(Pokemon.id == winner_poke_id)
            )
            winner_poke = winner_poke_result.scalar_one()
            lines.append(f"\n{winner_poke.display_name} leveled up to Lv.{winner_poke.level}!")
        if learned_moves:
            for mv in learned_moves:
                lines.append(f"{mv} was learned!")
        
        await session.commit()
        
        # Quest progress for battle win (was missing)
        from telemon.core.quests import update_quest_progress
        battle_quest_completed = await update_quest_progress(session, move_result["winner_id"], "battle_win")
        if battle_quest_completed:
            await session.commit()
            for q in battle_quest_completed:
                lines.append(f"📋 Quest complete: {q.description} (+{q.reward_coins:,} {CURRENCY_SHORT})")

        # Achievement hooks for battle win
        from telemon.core.achievements import check_achievements, format_achievement_notification
        battle_achs = await check_achievements(session, move_result["winner_id"], "battle_win")
        if battle_achs:
            await session.commit()
            lines.append(format_achievement_notification(battle_achs))

        # Team XP hook for battle win
        try:
            if winner.team_id:
                from telemon.core.teams import add_team_xp
                xp_added, new_lvl, leveled = await add_team_xp(session, winner.team_id, "battle_win")
                if leveled:
                    lines.append(f"\nYour team leveled up to Lv.{new_lvl}!")
        except Exception as e:
            logger.warning("Team XP award failed on PvP battle win", error=str(e))

        winner_name = winner.username or f"User {winner.telegram_id}"
        
        lines.extend([
            "",
            f"<b>{move_result['defender'].species.name} fainted!</b>",
            "",
            f"<b>@{winner_name} wins!</b>",
            f"Rewards: {move_result['winner_xp']} XP, {move_result['winner_coins']} {CURRENCY_SHORT}",
        ])
        
        logger.info(
            "Battle completed",
            battle_id=str(battle.id),
            winner_id=move_result["winner_id"],
            turns=battle.current_turn,
        )
        
        await callback.message.edit_text("\n".join(lines))
    else:
        # Battle continues - refresh and show next player's turn
        await session.refresh(battle)
        
        status = await format_battle_status(session, battle)
        
        builder = build_move_keyboard(battle, battle.whose_turn)
        
        lines.extend(["", status])
        
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=builder.as_markup(),
        )
    
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("battle:forfeit:"))
async def callback_forfeit_battle(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle battle forfeit."""
    if not callback.data:
        return
    
    battle_id = callback.data.split(":")[2]
    
    result = await session.execute(
        select(Battle).where(Battle.id == battle_id)
    )
    battle = result.scalar_one_or_none()
    
    if not battle:
        await callback.answer("Battle not found!", show_alert=True)
        return
    
    if user.telegram_id not in [battle.player1_id, battle.player2_id]:
        await callback.answer("You're not in this battle!", show_alert=True)
        return
    
    if battle.status not in [BattleStatus.PENDING, BattleStatus.ACTIVE]:
        await callback.answer("This battle has already ended!", show_alert=True)
        return
    
    winner_id = await forfeit_battle(session, battle, user.telegram_id)
    
    winner_result = await session.execute(
        select(User).where(User.telegram_id == winner_id)
    )
    winner = winner_result.scalar_one()
    winner_name = winner.username or f"User {winner_id}"
    
    forfeiter_name = user.username or f"User {user.telegram_id}"
    
    logger.info(
        "Battle forfeited",
        battle_id=str(battle.id),
        forfeiter_id=user.telegram_id,
        winner_id=winner_id,
    )
    
    await callback.message.edit_text(
        f" @{forfeiter_name} forfeited!\n\n"
        f"@{winner_name} wins by forfeit!"
    )
    await callback.answer("Battle forfeited!")


@router.message(Command("forfeit"))
async def cmd_forfeit(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /forfeit command."""
    # Check PvE first
    if user.telegram_id in _pve_battles:
        del _pve_battles[user.telegram_id]
        await message.answer("You fled from the battle!")
        return
    
    battle = await get_active_battle(session, user.telegram_id)
    
    if not battle:
        await message.answer(" You don't have an active battle!")
        return
    
    winner_id = await forfeit_battle(session, battle, user.telegram_id)
    
    winner_result = await session.execute(
        select(User).where(User.telegram_id == winner_id)
    )
    winner = winner_result.scalar_one()
    winner_name = winner.username or f"User {winner_id}"
    
    await message.answer(
        f" You forfeited the battle!\n"
        f"@{winner_name} wins!"
    )


# =================================================================
# PvE Battle Handlers — Wild encounters and NPC trainers
# =================================================================


def format_pve_status(state: dict) -> str:
    """Format PvE battle status display."""
    player = state["player"]
    enemy = state["enemy"]
    enemy_label = state.get("enemy_label", "Wild Pokemon")
    turn = state["turn"]

    lines = [
        f"<b>{enemy_label} — Turn {turn}</b>\n",
        f"<b>You</b>",
        f"{player['name']} Lv.{player['level']}",
        f"HP: {_pve_hp_bar(player['hp'], player['max_hp'])}",
        "",
        f"<b>Opponent</b>",
        f"{enemy['name']} Lv.{enemy['level']}",
        f"HP: {_pve_hp_bar(enemy['hp'], enemy['max_hp'])}",
    ]

    return "\n".join(lines)


def _pve_hp_bar(current: int, maximum: int, length: int = 10) -> str:
    filled = int((current / maximum) * length) if maximum > 0 else 0
    empty = length - filled
    return f"[{'█' * filled}{'░' * empty}] {current}/{maximum}"


def build_pve_move_keyboard(state: dict, user_id: int) -> InlineKeyboardBuilder:
    """Build move buttons for PvE battle."""
    builder = InlineKeyboardBuilder()

    for i, move in enumerate(state["player"]["moves"]):
        move_text = f"{move['name']} ({move['type'].title()})"
        builder.button(text=move_text, callback_data=f"pve:move:{user_id}:{i}")



    builder.button(text="Flee", callback_data=f"pve:flee:{user_id}", style="danger")
    builder.adjust(2)

    return builder


async def cmd_battle_wild(message: Message, session: AsyncSession, user: User) -> None:
    """Start a wild Pokemon battle."""
    # Check if user already in battle
    if user.telegram_id in _pve_battles:
        await message.answer(
            "You're already in a battle!\nUse the move buttons or /forfeit to end it."
        )
        return

    active_pvp = await get_active_battle(session, user.telegram_id)
    if active_pvp:
        await message.answer("You're already in a PvP battle!")
        return

    # Get player's selected Pokemon
    if not user.selected_pokemon_id:
        await message.answer("You need to select a Pokemon first!\nUse /select [#]")
        return

    poke_result = await session.execute(
        select(Pokemon).where(Pokemon.id == user.selected_pokemon_id)
    )
    player_poke = poke_result.scalar_one_or_none()
    if not player_poke:
        await message.answer("Your selected Pokemon was not found!")
        return

    # Pick random wild species
    from telemon.core.spawning.engine import get_random_species

    wild_species = await get_random_species(session)
    if not wild_species:
        await message.answer("Could not find any wild Pokemon!")
        return

    # Wild Pokemon level is close to player's level (+-5)
    wild_level = max(1, min(MAX_LEVEL, player_poke.level + random.randint(-5, 5)))

    # Build participants
    player_part = await build_pve_participant_from_pokemon_db(session, player_poke)
    enemy_part = build_pve_participant_from_species(wild_species, wild_level, iv_value=10)

    # Determine who goes first
    player_first = player_part.speed >= enemy_part.speed



    # Store PvE battle state in memory
    state = {
        "player": {
            "name": player_part.name,
            "level": player_part.level,
            "type1": player_part.type1,
            "type2": player_part.type2,
            "hp": player_part.hp,
            "max_hp": player_part.max_hp,
            "attack": player_part.attack,
            "defense": player_part.defense,
            "sp_attack": player_part.sp_attack,
            "sp_defense": player_part.sp_defense,
            "speed": player_part.speed,
            "moves": player_part.moves,
            "ability": player_part.ability,
        },
        "enemy": {
            "name": enemy_part.name,
            "level": enemy_part.level,
            "type1": enemy_part.type1,
            "type2": enemy_part.type2,
            "hp": enemy_part.hp,
            "max_hp": enemy_part.max_hp,
            "attack": enemy_part.attack,
            "defense": enemy_part.defense,
            "sp_attack": enemy_part.sp_attack,
            "sp_defense": enemy_part.sp_defense,
            "speed": enemy_part.speed,
            "moves": enemy_part.moves,
            "ability": enemy_part.ability,
        },
        "pokemon_id": str(player_poke.id),
        "enemy_species_id": wild_species.national_dex,
        "enemy_label": f"Wild {wild_species.name}",
        "turn": 1,
        "player_first": player_first,
        "mode": "wild",
        "reward_multiplier": 1.0,
        "coin_reward_base": 50 + wild_level * 5,

    }

    _pve_battles[user.telegram_id] = state

    status = format_pve_status(state)
    builder = build_pve_move_keyboard(state, user.telegram_id)

    await message.answer(
        f"A wild <b>{wild_species.name}</b> (Lv.{wild_level}) appeared!\n\n"
        f"{status}",
        reply_markup=builder.as_markup(),
    )

    logger.info(
        "PvE wild battle started",
        user_id=user.telegram_id,
        player_pokemon=player_poke.display_name,
        wild_species=wild_species.name,
        wild_level=wild_level,
    )


async def cmd_battle_npc(
    message: Message, session: AsyncSession, user: User, args: list[str]
) -> None:
    """Start an NPC trainer battle."""
    # If no trainer specified, show list
    if len(args) < 3:
        lines = [
            "<b>NPC Trainers</b>\n",
            "Challenge famous trainers to earn extra XP and coins!\n",
            "<b>Gym Leaders:</b>",
        ]
        for key, data in NPC_TRAINERS.items():
            mult = data["reward_multiplier"]
            lines.append(f"  /battle npc {key} — {data['title']} (x{mult} rewards)")
        lines.append("\n<i>Trainer Pokemon level scales with yours.</i>")

        await message.answer("\n".join(lines))
        return

    trainer_key = args[2].lower()

    if trainer_key not in NPC_TRAINERS:
        valid = ", ".join(NPC_TRAINERS.keys())
        await message.answer(
            f"Unknown trainer! Available: {valid}\n"
            "Use /battle npc to see the full list."
        )
        return

    # Check if already in battle
    if user.telegram_id in _pve_battles:
        await message.answer(
            "You're already in a battle!\n"
            "Use the move buttons or /forfeit to end it."
        )
        return

    active_pvp = await get_active_battle(session, user.telegram_id)
    if active_pvp:
        await message.answer("You're already in a PvP battle!")
        return

    # Get player's Pokemon
    if not user.selected_pokemon_id:
        await message.answer("You need to select a Pokemon first!\nUse /select [#]")
        return

    poke_result = await session.execute(
        select(Pokemon).where(Pokemon.id == user.selected_pokemon_id)
    )
    player_poke = poke_result.scalar_one_or_none()
    if not player_poke:
        await message.answer("Your selected Pokemon was not found!")
        return

    trainer_data = NPC_TRAINERS[trainer_key]

    # Get NPC's species
    species_result = await session.execute(
        select(PokemonSpecies).where(
            PokemonSpecies.national_dex == trainer_data["species_id"]
        )
    )
    npc_species = species_result.scalar_one_or_none()
    if not npc_species:
        await message.answer("Error loading NPC trainer data!")
        return

    # NPC level scales with player + offset
    npc_level = min(MAX_LEVEL, player_poke.level + trainer_data["level_offset"])

    # Build participants
    player_part = await build_pve_participant_from_pokemon_db(session, player_poke)
    enemy_part = build_pve_participant_from_species(npc_species, npc_level, iv_value=20)

    player_first = player_part.speed >= enemy_part.speed



    state = {
        "player": {
            "name": player_part.name,
            "level": player_part.level,
            "type1": player_part.type1,
            "type2": player_part.type2,
            "hp": player_part.hp,
            "max_hp": player_part.max_hp,
            "attack": player_part.attack,
            "defense": player_part.defense,
            "sp_attack": player_part.sp_attack,
            "sp_defense": player_part.sp_defense,
            "speed": player_part.speed,
            "moves": player_part.moves,
            "ability": player_part.ability,
        },
        "enemy": {
            "name": enemy_part.name,
            "level": enemy_part.level,
            "type1": enemy_part.type1,
            "type2": enemy_part.type2,
            "hp": enemy_part.hp,
            "max_hp": enemy_part.max_hp,
            "attack": enemy_part.attack,
            "defense": enemy_part.defense,
            "sp_attack": enemy_part.sp_attack,
            "sp_defense": enemy_part.sp_defense,
            "speed": enemy_part.speed,
            "moves": enemy_part.moves,
            "ability": enemy_part.ability,
        },
        "pokemon_id": str(player_poke.id),
        "enemy_species_id": npc_species.national_dex,
        "enemy_label": trainer_data["title"],
        "turn": 1,
        "player_first": player_first,
        "mode": "npc",
        "npc_key": trainer_key,
        "reward_multiplier": trainer_data["reward_multiplier"],
        "coin_reward_base": int((80 + npc_level * 8) * trainer_data["reward_multiplier"]),

    }

    _pve_battles[user.telegram_id] = state

    status = format_pve_status(state)
    builder = build_pve_move_keyboard(state, user.telegram_id)

    await message.answer(
        f"<b>{trainer_data['title']}</b> challenges you!\n"
        f"<i>\"{trainer_data['quote']}\"</i>\n\n"
        f"{trainer_data['title']} sends out <b>{npc_species.name}</b> (Lv.{npc_level})!\n\n"
        f"{status}",
        reply_markup=builder.as_markup(),
    )

    logger.info(
        "PvE NPC battle started",
        user_id=user.telegram_id,
        player_pokemon=player_poke.display_name,
        npc=trainer_key,
        npc_level=npc_level,
    )


def _dict_to_participant(d: dict) -> PveParticipant:
    """Convert a state dict back to PveParticipant."""
    return PveParticipant(
        name=d["name"],
        level=d["level"],
        type1=d["type1"],
        type2=d["type2"],
        hp=d["hp"],
        max_hp=d["max_hp"],
        attack=d["attack"],
        defense=d["defense"],
        sp_attack=d["sp_attack"],
        sp_defense=d["sp_defense"],
        speed=d["speed"],
        moves=d["moves"],
        ability=d.get("ability", ""),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("pve:move:"))
async def callback_pve_move(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle PvE move selection."""
    if not callback.data:
        return

    parts = callback.data.split(":")
    target_user_id = int(parts[2])
    move_index = int(parts[3])

    if user.telegram_id != target_user_id:
        await callback.answer("This is not your battle!", show_alert=True)
        return

    state = _pve_battles.get(user.telegram_id)
    if not state:
        await callback.answer("No active battle found!", show_alert=True)
        return

    player_part = _dict_to_participant(state["player"])
    enemy_part = _dict_to_participant(state["enemy"])

    moves = state["player"]["moves"]
    if move_index >= len(moves):
        move_index = 0
    player_move = moves[move_index]

    lines = []

    # Player attacks first (or based on speed)
    if state["player_first"]:
        # Player attacks
        result = pve_calculate_damage(player_part, enemy_part, player_move)
        lines.append(result.message)
        if result.damage > 0:
            lines.append(f"Dealt <b>{result.damage}</b> damage!")
        state["enemy"]["hp"] = max(0, state["enemy"]["hp"] - result.damage)

        # Check if enemy fainted
        if state["enemy"]["hp"] <= 0:
            await _handle_pve_win(callback, session, user, state, lines)
            return

        # Enemy counter-attacks
        enemy_move = random.choice(state["enemy"]["moves"])
        # Refresh participants with current HP
        enemy_part_updated = _dict_to_participant(state["enemy"])
        player_part_updated = _dict_to_participant(state["player"])

        enemy_result = pve_calculate_damage(enemy_part_updated, player_part_updated, enemy_move)
        lines.append("")
        lines.append(enemy_result.message)
        if enemy_result.damage > 0:
            lines.append(f"You took <b>{enemy_result.damage}</b> damage!")
        state["player"]["hp"] = max(0, state["player"]["hp"] - enemy_result.damage)

        if state["player"]["hp"] <= 0:
            await _handle_pve_loss(callback, user, state, lines)
            return
    else:
        # Enemy attacks first
        enemy_move = random.choice(state["enemy"]["moves"])
        enemy_result = pve_calculate_damage(enemy_part, player_part, enemy_move)
        lines.append(enemy_result.message)
        if enemy_result.damage > 0:
            lines.append(f"You took <b>{enemy_result.damage}</b> damage!")
        state["player"]["hp"] = max(0, state["player"]["hp"] - enemy_result.damage)

        if state["player"]["hp"] <= 0:
            await _handle_pve_loss(callback, user, state, lines)
            return

        # Player attacks
        player_part_updated = _dict_to_participant(state["player"])
        enemy_part_updated = _dict_to_participant(state["enemy"])

        result = pve_calculate_damage(player_part_updated, enemy_part_updated, player_move)
        lines.append("")
        lines.append(result.message)
        if result.damage > 0:
            lines.append(f"Dealt <b>{result.damage}</b> damage!")
        state["enemy"]["hp"] = max(0, state["enemy"]["hp"] - result.damage)

        if state["enemy"]["hp"] <= 0:
            await _handle_pve_win(callback, session, user, state, lines)
            return

    # Battle continues
    state["turn"] += 1

    status = format_pve_status(state)
    builder = build_pve_move_keyboard(state, user.telegram_id)

    lines.extend(["", status])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


async def _handle_pve_win(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    state: dict,
    lines: list[str],
) -> None:
    """Handle player winning a PvE battle."""
    enemy_name = state["enemy"]["name"]
    enemy_level = state["enemy"]["level"]
    player_level = state["player"]["level"]
    pokemon_id = state["pokemon_id"]
    mode = state["mode"]
    multiplier = state["reward_multiplier"]

    # Calculate rewards
    if mode == "npc":
        xp_reward = calculate_npc_battle_xp(player_level, enemy_level, multiplier)
    else:
        xp_reward = calculate_wild_battle_xp(player_level, enemy_level)
    xp_reward = apply_xp_boost(xp_reward, user.xp_boost_until)
    coin_reward = state["coin_reward_base"]

    # Apply rewards
    user.balance += coin_reward
    xp_added, levels_gained, learned_moves = await add_xp_to_pokemon(session, pokemon_id, xp_reward)

    # Battle win stats (counts for PvE too)
    user.battle_wins += 1
    await session.commit()

    lines.extend([
        "",
        f"<b>{enemy_name} fainted!</b>",
        "",
        f"<b>You win!</b>",
        f"Rewards: {xp_reward} XP, {coin_reward} {CURRENCY_SHORT}",
    ])

    if levels_gained:
        poke_result = await session.execute(
            select(Pokemon).where(Pokemon.id == pokemon_id)
        )
        poke = poke_result.scalar_one()
        lines.append(f"{poke.display_name} leveled up to Lv.{poke.level}!")
    if learned_moves:
        for mv in learned_moves:
            lines.append(f"{mv} was learned!")

    # Quest progress
    from telemon.core.quests import update_quest_progress
    quest_completed = await update_quest_progress(session, user.telegram_id, "battle_win")
    if quest_completed:
        await session.commit()
        for q in quest_completed:
            lines.append(f"📋 Quest complete: {q.description} (+{q.reward_coins:,} {CURRENCY_SHORT})")

    # Achievement hooks
    from telemon.core.achievements import check_achievements, format_achievement_notification
    battle_achs = await check_achievements(session, user.telegram_id, "battle_win")
    if battle_achs:
        await session.commit()
        lines.append(format_achievement_notification(battle_achs))

    # Team XP hook for PvE battle win
    try:
        if user.team_id:
            from telemon.core.teams import add_team_xp
            xp_added, new_lvl, leveled = await add_team_xp(session, user.team_id, "battle_win")
            if leveled:
                lines.append(f"\nYour team leveled up to Lv.{new_lvl}!")
    except Exception as e:
        logger.warning("Team XP award failed on PvE battle win", error=str(e))

    # Clean up
    del _pve_battles[user.telegram_id]

    await callback.message.edit_text("\n".join(lines))
    await callback.answer("You won!")

    logger.info(
        "PvE battle won",
        user_id=user.telegram_id,
        mode=mode,
        enemy=enemy_name,
        xp=xp_reward,
        coins=coin_reward,
    )


async def _handle_pve_loss(
    callback: CallbackQuery,
    user: User,
    state: dict,
    lines: list[str],
) -> None:
    """Handle player losing a PvE battle."""
    player_name = state["player"]["name"]
    enemy_label = state.get("enemy_label", "Wild Pokemon")

    lines.extend([
        "",
        f"<b>{player_name} fainted!</b>",
        "",
        f"You were defeated by <b>{enemy_label}</b>!",
        "<i>Better luck next time, trainer!</i>",
    ])

    # Clean up
    del _pve_battles[user.telegram_id]

    await callback.message.edit_text("\n".join(lines))
    await callback.answer("You lost!")


@router.callback_query(lambda c: c.data and c.data.startswith("pve:flee:"))
async def callback_pve_flee(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle fleeing from PvE battle."""
    if not callback.data:
        return

    target_user_id = int(callback.data.split(":")[2])

    if user.telegram_id != target_user_id:
        await callback.answer("This is not your battle!", show_alert=True)
        return

    if user.telegram_id in _pve_battles:
        del _pve_battles[user.telegram_id]

    await callback.message.edit_text("You fled from the battle!")
    await callback.answer("Fled!")



