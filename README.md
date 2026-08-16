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

4. **Database URL on Heroku**
   ```bash
   # Heroku Postgres usually provides DATABASE_URL as postgres://...
   # The app automatically normalizes it to postgresql+asyncpg://... at runtime.
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
