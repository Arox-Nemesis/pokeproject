# Telemon

A Pokemon-style game bot for Telegram, inspired by Poketwo (Discord).

## Features

- **Wild Pokemon Spawns** - Pokemon spawn in group chats based on activity
- **Catching System** - Identify and catch Pokemon by name
- **Pokemon Collection** - Manage your caught Pokemon with filters and sorting
- **Trading** - Trade Pokemon and Telecoins with other trainers
- **PvP Battles** - Turn-based battles with full type effectiveness
- **Global Market** - Buy and sell Pokemon on the marketplace
- **Shop & Items** - Purchase evolution stones, battle items, and more
- **Shiny Hunting** - Build chains to improve shiny odds
- **Group-only gameplay** - Commands and callbacks are processed in Telegram groups only
- **Force subscription** - Optionally require users to join a channel/group before playing

## Tech Stack

- **Python 3.11+** with async/await
- **aiogram 3.x** - Telegram Bot framework
- **PostgreSQL** - Primary database
- **Redis** - Caching and FSM storage
- **SQLAlchemy 2.0** - Async ORM
- **Alembic** - Database migrations

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 14+
- Redis 7+
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### Installation

1. **Clone and setup environment**
   ```bash
   cd Pikamon
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -e .
   ```

2. **Start databases with Docker**
   ```bash
   docker-compose up -d
   ```

3. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env and add your BOT_TOKEN
   # Optional: set TELEGRAM_API_ID and TELEGRAM_API_HASH only if you add MTProto/client integrations
   # Optional: set FORCE_SUB_ENABLED=true, FORCE_SUB_CHAT_ID, and FORCE_SUB_URL
   ```

4. **Managed database/Redis URLs**
   ```bash
   # Heroku/Neon Postgres URLs such as postgres://... or postgresql://...?sslmode=require
   # are automatically normalized to postgresql+asyncpg://... at runtime.
   # sslmode=require becomes asyncpg SSL settings, and unsupported asyncpg
   # query options such as channel_binding are stripped before connecting.

   # Upstash Redis TLS URLs should use rediss://...; redis://...upstash.io URLs
   # are automatically upgraded to rediss://... for redis-py.
   ```

5. **Run database migrations**
   ```bash
   alembic upgrade head
   ```

6. **Import Pokemon data**
   ```bash
   # Download data from PokeAPI (takes 15-30 minutes)
   python scripts/import_pokemon_data.py
   
   # Seed the database
   python scripts/seed_database.py
   ```

7. **Start the bot**
   ```bash
   python -m telemon.main
   ```

## Project Structure

```
telemon/
├── src/telemon/
│   ├── bot/              # Telegram bot (handlers, keyboards, middlewares)
│   ├── core/             # Business logic (spawning, battle, trading)
│   ├── database/         # SQLAlchemy models and repositories
│   └── utils/            # Shared utilities
├── data/                 # Static Pokemon data (JSON)
├── scripts/              # Data import and seeding scripts
├── alembic/              # Database migrations
└── tests/                # Test suite
```

## Telegram configuration

The bot runtime uses aiogram and only requires `BOT_TOKEN` for Telegram Bot API access. `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are available as optional config variables for future MTProto/client integrations, but the current bot code does not consume them.

## Telegram access rules

By default, the bot only responds in Telegram groups and supergroups. Private chats receive an instruction to add the bot to a group.

To require every player to join a channel or group before using the bot, set:

```env
FORCE_SUB_ENABLED=true
FORCE_SUB_CHAT_ID=@yourchannel
FORCE_SUB_URL=https://t.me/yourchannel
```

The bot must be able to call `getChatMember` for `FORCE_SUB_CHAT_ID`; add it to the required group/channel with suitable access.

## Commands

### General
- `/start` - Start the bot
- `/help` - Show commands
- `/profile` - View your profile
- `/balance` - Check Telecoins
- `/daily` - Claim daily reward

### Pokemon
- `/catch <name>` - Catch a Pokemon
- `/hint` - Get a name hint
- `/pokemon` - List your Pokemon
- `/info [id]` - View details
- `/select <id>` - Set active Pokemon

### Trading & Market
- `/trade @user` - Start a trade
- `/market search` - Browse market
- `/shop` - View shop

### Battle
- `/duel @user` - Challenge to battle

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
ruff format src tests

# Type check
mypy src
```

## License

This project is for educational purposes. Pokemon is a trademark of Nintendo/Game Freak/The Pokemon Company.

## Contributing

Contributions are welcome! Please read the development plan in `DEVELOPMENT_PLAN.md`.

## Owner controls and per-group settings

Set these environment variables before deploying:

```env
OWNER_ID=123456789                 # your numeric Telegram user ID (required for /control and owner tools)
OWNER_GROUP_ID=-1001234567890      # optional: only this group can host /control
INCENSE_SPAWN_COUNT=50
INCENSE_SPAWN_INTERVAL_SECONDS=10  # seconds between incense spawn attempts
```

`/control` is an owner-only inline control panel. **Default** changes the persistent baseline for all groups. Select **Add**, then send a numeric Telegram group ID (or use `/control add -100...`) to create a group entry; it inherits Default values until you change one of its settings. Group overrides affect only that group and persist in PostgreSQL. The panel manages spawn thresholds/timers/cooldowns, catch and daily rewards, market fee, and incense settings.

Owner category spawns remain group-only. Use standard filters such as `/spawn legendary gen:4`; configured spawn admins require their matching permissions, while `OWNER_ID` has all permissions. Set `OWNER_GROUP_ID` only to limit the `/control` panel to one group; owner `/spawn` commands remain available in every group.

## Collection and form commands

`/pokemon <name>` lists every matching caught Pokémon while preserving its original catch serial number. Collection filters include `rare`, `legendary`, `mythical`, `ultra_beast`, `unique`, `shiny`, `type:<type>`, and `gen:<number>`. Use `sort:level`, `sort:iv`, `sort:dex`, `sort:name`, or `sort:rarity`.

Regional forms, the existing evolution-item catalogue, and Mega Evolutions are already supported. Use `/shop` to obtain evolution items and Mega Stones, `/evolve` for normal item/level evolutions, and `/mega` / `/demega` for supported Mega forms. Gigantamax is not enabled because the current species data and battle system do not provide a complete, balanced Gigantamax form set; this avoids offering transformations that cannot function correctly in battles.

## Quiet-group spawning and owner batches

Groups no longer need activity to begin spawning. A group’s first wild Pokémon is eligible **three minutes** after the bot joins. After every delivered wild Pokémon, the bot reserves a **10-minute catch window** and then waits a randomized **3–9 minutes** before the next wild spawn. Message-based spawning still works as an additional trigger.

`/spawn` category and batch spawning is authenticated by `OWNER_ID`, not by `OWNER_GROUP_ID`. The owner can use `/spawn legendary gen4`, `/spawn type:fire 5`, `/spawn mythical 10`, `/spawn ultra_rare 3`, or `/spawn --shiny 2`. Batch Pokémon are queued persistently and appear one at a time after the preceding forced spawn is caught or expires, preventing overlapping uncapturable spawns.

Pokédex lookups accept the conventional number notation: `/pokedex #25` and `/pokedex 25` both open Pikachu’s entry.

Cosmoem has a dedicated Lunala route: at level 53 or above, use a **Lunala Stone** (`/evolve <serial> lunala stone`). It is sold in `/shop` for 20,000 coins and is consumed by the evolution.

### Deployment conflict resolution

When GitHub reports a conflict, resolve it by retaining the newest version of the affected file from this branch—especially `src/telemon/bot/handlers/admin.py`, `src/telemon/main.py`, and `README.md`—then redeploy that merge commit. An older conflict side can restore the removed owner-group `/spawn` restriction. The current code authorizes `/spawn` by `OWNER_ID`; `OWNER_GROUP_ID` applies only to the owner control panel.

The Pokédex Forms page now reads bundled PokeAPI form metadata, so it lists alternate forms such as **Lycanroc Midday, Midnight, and Dusk**, even for deployments whose database was seeded before alternate-form rows were available.
