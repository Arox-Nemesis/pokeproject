"""Database models package."""

from telemon.database.models.base import Base, TimestampMixin
from telemon.database.models.battle import Battle, BattleStatus
from telemon.database.models.group import Group
from telemon.database.models.item import InventoryItem, Item
from telemon.database.models.market import ListingStatus, MarketListing
from telemon.database.models.move import Move, PokemonLearnset
from telemon.database.models.pokedex import PokedexEntry
from telemon.database.models.pokemon import Pokemon
from telemon.database.models.quest import UserQuest
from telemon.database.models.spawn import ActiveSpawn
from telemon.database.models.spawn_admin import SpawnAdmin
from telemon.database.models.species import PokemonSpecies
from telemon.database.models.trade import Trade, TradeHistory, TradeStatus
from telemon.database.models.user import User
from telemon.database.models.wondertrade import WonderTrade
from telemon.database.models.achievement import UserAchievement
from telemon.database.models.breeding import DaycareSlot, PokemonEgg
from telemon.database.models.bot_config import BotConfig
from telemon.database.models.team import Team

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    # Core
    "User",
    "Pokemon",
    "PokemonSpecies",
    "Group",
    # Static data
    "Move",
    "PokemonLearnset",
    "Item",
    "InventoryItem",
    # Features
    "ActiveSpawn",
    "SpawnAdmin",
    "Trade",
    "TradeHistory",
    "TradeStatus",
    "MarketListing",
    "ListingStatus",
    "Battle",
    "BattleStatus",
    "PokedexEntry",
    "UserQuest",
    "WonderTrade",
    "UserAchievement",
    # Breeding
    "DaycareSlot",
    "PokemonEgg",
    # Teams
    "Team",
    # Config
    "BotConfig",
]
