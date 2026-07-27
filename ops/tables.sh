#!/usr/bin/env bash
# Single source of truth for which tables hold irreplaceable data.
#
# Sourced by logical_dump.sh, check_anomaly.py (via --tables), restore_*.sh
# and restore_drill.sh. If you add a model under
# src/telemon/database/models/, add its table here.

# ---------------------------------------------------------------------------
# USER DATA — irreplaceable. There is no third-party source for any of this.
# Dumped every 15 minutes and guarded by the anomaly gate.
# ---------------------------------------------------------------------------
USER_TABLES=(
  users
  pokemon
  pokemon_eggs
  daycare_slots
  inventory_items
  pokedex_entries
  user_achievements
  user_quests
  teams
  trades
  trade_history
  wonder_trades
  market_listings
  battles
  groups
  spawn_admins
  bot_config
  alembic_version
)

# ---------------------------------------------------------------------------
# REFERENCE DATA — regenerable from scripts/fetch_*.py against PokeAPI.
# Included in the 6-hourly full dump only; excluded from the frequent
# user-data dumps to keep them small and fast as the game grows.
# ---------------------------------------------------------------------------
REFERENCE_TABLES=(
  pokemon_species
  moves
  pokemon_learnsets
  items
)

# ---------------------------------------------------------------------------
# TRANSIENT — in-flight state, worthless after a restart. WAL covers it.
# ---------------------------------------------------------------------------
TRANSIENT_TABLES=(
  active_spawns
)

# Tables whose row count must never drop sharply. A shrink here means either a
# real incident or a bug, and either way we do not want it silently becoming
# the new baseline.
GUARDED_TABLES=(
  users
  pokemon
  pokedex_entries
  inventory_items
  user_achievements
  trade_history
)
