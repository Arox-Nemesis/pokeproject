"""Handler registration."""

from aiogram import Dispatcher

from telemon.bot.handlers import (
    achievements,
    admin,
    battle,
    breeding,
    catch,
    help_cmd,
    leaderboard,
    market,
    mega,
    moves,
    pokedex,
    pokemon,
    profile,
    quests,
    runtime,
    shinyhunt,
    shop,
    spawn,
    start,
    team,
    trade,
    wondertrade,
)


def register_all_handlers(dp: Dispatcher) -> None:
    """Register all handlers with the dispatcher."""
    # Core handlers
    dp.include_router(start.router)
    dp.include_router(help_cmd.router)
    dp.include_router(profile.router)
    dp.include_router(leaderboard.router)

    # Pokemon handlers
    dp.include_router(catch.router)
    dp.include_router(pokemon.router)
    dp.include_router(moves.router)
    dp.include_router(pokedex.router)
    dp.include_router(shinyhunt.router)

    # Battle handlers
    dp.include_router(battle.router)
    dp.include_router(mega.router)

    # Economy handlers
    dp.include_router(trade.router)
    dp.include_router(market.router)
    dp.include_router(shop.router)

    # Quest handlers
    dp.include_router(quests.router)

    # Wonder Trade
    dp.include_router(wondertrade.router)

    # Achievements
    dp.include_router(achievements.router)

    # Breeding
    dp.include_router(breeding.router)

    # Teams / Guilds
    dp.include_router(team.router)

    # Admin handlers
    dp.include_router(admin.router)

    # Runtime restart/rebuild (owner-only)
    dp.include_router(runtime.router)

    # Spawn handler (MUST be last - catches all group messages)
    dp.include_router(spawn.router)


__all__ = ["register_all_handlers"]
