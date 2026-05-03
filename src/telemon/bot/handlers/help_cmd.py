"""Help command handlers with category-based inline keyboard."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telemon.config import BOT_NAME, CURRENCY_NAME, CURRENCY_SHORT
from telemon.core.constants import MAX_FRIENDSHIP, MAX_GIFT_AMOUNT, WT_COOLDOWN_SECONDS

router = Router(name="help")


# ---------------------------------------------------------------------------
# Category definitions
# ---------------------------------------------------------------------------

HELP_CATEGORIES = {
    "start": {
        "emoji": "🏠",
        "title": "Getting Started",
        "text": (
            "<b>Getting Started</b>\n\n"
            "/start - Start the bot, pick a starter\n"
            "/help - Show help menu\n"
            "/help [command] - Detailed help for a command"
        ),
    },
    "profile": {
        "emoji": "👤",
        "title": "Profile & Economy",
        "text": (
            "<b>Profile & Economy</b>\n\n"
            "/profile - View your trainer profile\n"
            f"/balance - Check your {CURRENCY_NAME}\n"
            "/daily - Claim daily reward (+friendship)\n"
            f"/gift @user [amount] - Send {CURRENCY_NAME}"
        ),
    },
    "catch": {
        "emoji": "🔴",
        "title": "Catching & Collection",
        "text": (
            "<b>Catching & Collection</b>\n\n"
            "/catch [name] - Catch a wild Pokemon\n"
            "/hint - Get a hint for the current spawn\n"
            "/pokemon - List your Pokemon\n"
            "/info [#] - View Pokemon details\n"
            "/select [#] - Select active Pokemon\n"
            "/nickname [name] - Rename selected\n"
            "/favorite [#] - Toggle favorite\n"
            "/release [#] - Release a Pokemon\n"
            "/release duplicates - Bulk release duplicates\n"
            "/release all [filters] - Bulk release with filters\n\n"
            "<i>Filters: shiny, type:fire, gen:3, sort:iv</i>"
        ),
    },
    "evolve": {
        "emoji": "⚡",
        "title": "Evolution & Training",
        "text": (
            "<b>Evolution & Training</b>\n\n"
            "/evolve [#] [item] - Evolve a Pokemon\n"
            "/pet [#] - Increase friendship\n\n"
            "<b>Moves</b>\n"
            "/moves [#] - View known moves\n"
            "/learn [move] - Teach a move\n"
            "/forget [move] - Forget a move\n"
            "/learnable [#] - See learnable moves"
        ),
    },
    "battle": {
        "emoji": "⚔️",
        "title": "Battle",
        "text": (
            "<b>Battle</b>\n\n"
            "/duel @user - Challenge to PvP battle\n"
            "/battle wild - Fight a wild Pokemon\n"
            "/battle npc - List NPC trainers\n"
            "/battle npc [name] - Fight a trainer\n"
            "/forfeit - End battle early\n\n"
            "<b>Mega Evolution</b>\n"
            "/mega - Check selected Pokemon\n"
            "/mega list - List all mega evolutions\n\n"
            "<i>Give your Pokemon a mega stone (from /shop) and "
            "press Mega Evolve during battle!</i>"
        ),
    },
    "trade": {
        "emoji": "🤝",
        "title": "Trading & Market",
        "text": (
            "<b>Trading</b>\n"
            "/trade @user - Start a trade\n"
            "/trade add/remove/confirm/cancel\n\n"
            "<b>Market</b>\n"
            "/market - Browse marketplace\n"
            "/market sell [#] [price] - List for sale\n"
            "/market buy [id] - Buy a listing\n\n"
            "<b>Wonder Trade</b>\n"
            "/wt [#] - Deposit for random swap\n"
            "/wt status - Check pool status"
        ),
    },
    "shop": {
        "emoji": "🛒",
        "title": "Shop & Items",
        "text": (
            "<b>Shop & Items</b>\n\n"
            "/shop - View the shop\n"
            "/buy [id] [qty] - Purchase an item\n"
            "/use [id] [#] - Use item on Pokemon\n"
            "/inventory - View your items\n"
            "/shopinfo [id] - Item details"
        ),
    },
    "quest": {
        "emoji": "📋",
        "title": "Quests & Achievements",
        "text": (
            "<b>Quests</b>\n"
            "/quest - View daily & weekly quests\n"
            "/quest claimall - Claim completed rewards\n\n"
            "<b>Achievements</b>\n"
            "/achievements - View your badges\n"
            "/badges - Same as /achievements"
        ),
    },
    "dex": {
        "emoji": "📕",
        "title": "Pokedex & Hunting",
        "text": (
            "<b>Pokedex & Hunting</b>\n\n"
            "/pokedex - Pokedex overview\n"
            "/pokedex list gen:3 - Browse by generation\n"
            "/pokedex caught/missing/shiny\n"
            "/pokedex [name/#] - Look up a Pokemon\n\n"
            "/shinyhunt [name] - Set shiny target\n"
            "/hunt status/stop/odds"
        ),
    },
    "breed": {
        "emoji": "🥚",
        "title": "Breeding",
        "text": (
            "<b>Breeding</b>\n\n"
            "/daycare - View daycare status\n"
            "/daycare add [#] - Place Pokemon in daycare\n"
            "/daycare remove [1/2] - Remove from slot\n"
            "/breed - Produce an egg\n"
            "/eggs - View eggs & hatch progress\n"
            "/hatch - Hatch ready eggs\n\n"
            "<i>Steps decrease from group messages.</i>"
        ),
    },
    "lb": {
        "emoji": "🏆",
        "title": "Leaderboards",
        "text": (
            "<b>Leaderboards</b>\n\n"
            "/leaderboard - View rankings\n"
            "/lb catches/wealth/pokedex/shiny/battles/rating/group\n"
            "/rank - Your ranking"
        ),
    },
    "team": {
        "emoji": "🛡️",
        "title": "Teams / Guilds",
        "text": (
            "<b>Teams / Guilds</b>\n\n"
            "/team - View your team\n"
            "/team create [name] [tag] - Create a team\n"
            "/team join [tag] - Join a team\n"
            "/team leave - Leave your team\n"
            "/team members - View members\n"
            "/team list - Browse all teams\n"
            "/team info [tag] - View a team\n"
            "/team kick/promote/demote - Reply to manage\n"
            "/team transfer - Transfer leadership\n"
            "/team disband - Delete your team\n"
            "/team tag [new] - Change tag\n"
            "/team desc [text] - Set description\n"
            "/team policy [open/invite_only]\n\n"
            "<i>Teams gain XP from catches, battles, and evolutions!</i>"
        ),
    },
}

# Ordered list for button layout
CATEGORY_ORDER = [
    "start", "profile", "catch", "evolve", "battle", "trade",
    "shop", "quest", "dex", "breed", "lb", "team",
]


# ---------------------------------------------------------------------------
# Detailed per-command help (unchanged from before)
# ---------------------------------------------------------------------------

COMMAND_HELP = {
    "catch": (
        "<b>/catch [pokemon_name]</b>\n"
        "Also: /c\n\n"
        "Catch a wild Pokemon that has spawned in the chat.\n"
        "You must type the correct name of the Pokemon.\n\n"
        "<b>Example:</b> /catch pikachu"
    ),
    "pokemon": (
        "<b>/pokemon</b>\n"
        "Also: /p\n\n"
        "View your Pokemon collection with filters and sorting.\n\n"
        "<b>Filters (both styles work):</b>\n"
        "shiny or --shiny — Show only shinies\n"
        "legendary or --legendary — Show legendaries\n"
        "name:char or --name char — Search by name\n"
        "type:fire or --type fire — Filter by type\n"
        "gen:3 or --gen 3 — Filter by generation\n"
        "fav or --favorites — Show only favorites\n\n"
        "<b>Sorting:</b>\n"
        "sort:iv — Sort by IV percentage\n"
        "sort:level — Sort by level\n"
        "sort:dex — Sort by Pokedex number\n"
        "sort:name — Sort alphabetically\n\n"
        "<b>Example:</b> /pokemon shiny type:fire sort:iv"
    ),
    "evolve": (
        "<b>/evolve [#] [item name]</b>\n\n"
        "Evolve a Pokemon. Works with index number or selected Pokemon.\n\n"
        "<b>Evolution Types:</b>\n"
        "Level-up: Just reach the required level, then /evolve\n"
        "Item: /evolve 1 fire stone (uses and consumes the item)\n"
        "Trade: /evolve 1 linking cord (uses Linking Cord from shop)\n"
        "Friendship: Raise friendship to 220+ with /pet, then /evolve\n\n"
        "<b>Example:</b> /evolve 1\n"
        "<b>Example:</b> /evolve 1 thunder stone"
    ),
    "trade": (
        "<b>/trade @user</b>\n\n"
        "Start a trade with another user.\n\n"
        "<b>Trade Commands:</b>\n"
        "/trade add [#] — Add Pokemon\n"
        f"/trade add coins [amount] — Add {CURRENCY_NAME}\n"
        "/trade remove [#] — Remove Pokemon\n"
        "/trade confirm — Confirm your side\n"
        "/trade cancel — Cancel the trade\n\n"
        "Both users must confirm for trade to complete.\n"
        "Trade evolutions trigger automatically!"
    ),
    "market": (
        "<b>/market search [filters]</b>\n\n"
        "Browse the global Pokemon marketplace.\n\n"
        "<b>Filters:</b>\n"
        "--name [text] — Search by name\n"
        "--type [type] — Filter by type\n"
        "--shiny — Shinies only\n"
        "--legendary — Legendaries only\n"
        "--iv [min]-[max] — IV percentage range\n\n"
        "<b>Example:</b> /market search --name charizard --shiny"
    ),
    "quest": (
        "<b>/quest</b>\n"
        "Also: /quests, /q\n\n"
        "View and manage your daily & weekly quests.\n\n"
        "/quest — View all active quests with progress\n"
        "/quest claimall — Claim all completed quest rewards\n\n"
        "Daily quests (3) reset at midnight UTC.\n"
        "Weekly quests (2) reset Monday midnight UTC."
    ),
    "shop": (
        "<b>/shop</b>\n\n"
        "Browse the item shop.\n\n"
        "<b>Item Categories:</b>\n"
        f"Evolution Stones (500 {CURRENCY_SHORT})\n"
        f"Evolution Items (1000-1500 {CURRENCY_SHORT})\n"
        f"Linking Cord (3000 {CURRENCY_SHORT})\n"
        f"Soothe Bell (2000 {CURRENCY_SHORT})\n"
        "Battle Items — Leftovers, Choice Band, etc.\n"
        f"Utility — Rare Candy (200 {CURRENCY_SHORT}), Incense\n"
        f"Special — Shiny Charm (50,000 {CURRENCY_SHORT})\n\n"
        "/buy [id] [qty] — Purchase items\n"
        "/shopinfo [id] — View item details\n"
        "/use [id] [#] — Use item on Pokemon"
    ),
    "gift": (
        "<b>/gift @user [amount]</b>\n"
        "Also: /give, /send\n\n"
        f"Send {CURRENCY_NAME} to another trainer.\n\n"
        f"/gift @friend 500 — Send 500 {CURRENCY_SHORT}\n"
        "Reply to a message + /gift 500 — Send to that user\n\n"
        f"Maximum: {MAX_GIFT_AMOUNT:,} {CURRENCY_SHORT} per transfer."
    ),
    "pet": (
        "<b>/pet [#]</b>\n\n"
        "Pet your Pokemon to increase its friendship.\n\n"
        "Base gain: +5 friendship per pet\n"
        "With Soothe Bell: +10 per pet\n\n"
        "Friendship is needed for certain evolutions.\n"
        f"Max friendship: {MAX_FRIENDSHIP}"
    ),
    "wondertrade": (
        "<b>/wt [pokemon#]</b>\n"
        "Also: /wondertrade\n\n"
        "Deposit a Pokemon and receive a random one from another trainer!\n\n"
        "/wt [pokemon#] — Deposit\n"
        "/wt status — Check pool status\n\n"
        "1 Pokemon in the pool at a time.\n"
        f"{WT_COOLDOWN_SECONDS // 60} minute cooldown between trades."
    ),
    "achievements": (
        "<b>/achievements</b>\n"
        "Also: /badges, /ach\n\n"
        "View your achievement progress.\n\n"
        "<b>Categories:</b> Catching, Shiny, Pokedex, Evolution, Battle, "
        "Trading, Daily Streak, Special, Wonder Trade\n\n"
        f"Each achievement grants a {CURRENCY_SHORT} reward when unlocked."
    ),
    "battle": (
        "<b>/battle</b>\n"
        "Also: /duel\n\n"
        "<b>PvP:</b> /duel @username\n\n"
        "<b>PvE - Wild:</b> /battle wild\n"
        f"Earn XP and {CURRENCY_NAME}.\n\n"
        "<b>PvE - NPC:</b> /battle npc [name]\n"
        "Fight gym leaders and champions.\n\n"
        "<b>During Battle:</b>\n"
        "Select moves with inline buttons.\n"
        "/forfeit — End battle early."
    ),
    "moves": (
        "<b>/moves [#]</b>\n\n"
        "View a Pokemon's known moves with full details.\n\n"
        "<b>Related:</b>\n"
        "/learn [move] — Learn a new move\n"
        "/forget [move] — Forget a move\n"
        "/learnable — See available moves"
    ),
    "learn": (
        "<b>/learn [move name]</b>\n\n"
        "Teach your selected Pokemon a new move.\n"
        "Maximum 4 moves — use /forget first to make room.\n\n"
        "<b>Example:</b> /learn flamethrower"
    ),
    "forget": (
        "<b>/forget [move name]</b>\n\n"
        "Forget a move from your selected Pokemon.\n"
        "Supports partial name matching.\n\n"
        "<b>Example:</b> /forget tackle"
    ),
    "learnable": (
        "<b>/learnable [#]</b>\n"
        "Also: /movelist\n\n"
        "Show all moves a Pokemon can learn at its current level.\n"
        "Moves already known are marked with [Known]."
    ),
    "daycare": (
        "<b>/daycare</b>\n\n"
        "Manage your Pokemon daycare for breeding.\n\n"
        "/daycare — View daycare status & compatibility\n"
        "/daycare add [#] — Place a Pokemon in daycare\n"
        "/daycare remove [1/2] — Remove from slot\n\n"
        "You can have up to 2 Pokemon in the daycare at a time."
    ),
    "breed": (
        "<b>/breed</b>\n\n"
        "Attempt to breed the two Pokemon in your daycare.\n\n"
        "<b>Requirements:</b>\n"
        "- 2 Pokemon in daycare\n"
        "- Compatible egg groups (or one is Ditto)\n"
        "- One male + one female (or Ditto + anything)\n\n"
        "3 IVs inherited from parents. Max 6 eggs.\n"
        "Steps decrease with group messages."
    ),
    "eggs": (
        "<b>/eggs</b>\n\n"
        "View all your Pokemon eggs with hatch progress.\n"
        "Steps are added automatically when you chat in groups."
    ),
    "hatch": (
        "<b>/hatch</b>\n\n"
        "Hatch all eggs that have reached 0 steps remaining.\n"
        "Hatched Pokemon start at Lv.1 with 120 friendship."
    ),
    "team": (
        "<b>/team [subcommand]</b>\n"
        "Also: /guild, /t\n\n"
        "Manage your team / guild.\n\n"
        "<b>Subcommands:</b>\n"
        "/team — View your team info\n"
        "/team create [name] [tag] — Create a new team\n"
        "/team join [tag] — Join a team\n"
        "/team leave — Leave your team\n"
        "/team members — View members (paginated)\n"
        "/team list — Browse all teams\n"
        "/team info [tag] — View another team\n"
        "/team kick — Kick a member (reply)\n"
        "/team promote — Promote to officer (reply)\n"
        "/team demote — Demote officer (reply)\n"
        "/team transfer — Transfer leadership (reply)\n"
        "/team disband — Delete the team\n"
        "/team tag [new] — Change tag (leader)\n"
        "/team desc [text] — Set description\n"
        "/team policy [open/invite_only] — Set join policy\n\n"
        "Teams level up from member catches, battles, and evolutions.\n"
        "Higher levels unlock more member slots."
    ),
}


# ---------------------------------------------------------------------------
# Build the main help keyboard
# ---------------------------------------------------------------------------

def build_help_keyboard() -> InlineKeyboardBuilder:
    """Build the category selection keyboard."""
    builder = InlineKeyboardBuilder()
    for key in CATEGORY_ORDER:
        cat = HELP_CATEGORIES[key]
        builder.button(
            text=f"{cat['emoji']} {cat['title']}",
            callback_data=f"help:{key}",
        )
    builder.adjust(2)  # 2 buttons per row
    return builder


HELP_OVERVIEW = (
    f"<b>{BOT_NAME} Help</b>\n\n"
    "Tap a category below to see its commands.\n"
    "Use <code>/help [command]</code> for detailed help on any command.\n\n"
    "<i>Most commands work with index numbers (/info 3) or selected Pokemon (/info).</i>"
)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command — show category keyboard or detailed help."""
    args = message.text.split() if message.text else []

    if len(args) > 1:
        command = args[1].lower().lstrip("/")
        if command in COMMAND_HELP:
            await message.answer(COMMAND_HELP[command])
            return
        # Try matching to a category
        if command in HELP_CATEGORIES:
            await message.answer(
                HELP_CATEGORIES[command]["text"],
                reply_markup=_back_keyboard().as_markup(),
            )
            return

    keyboard = build_help_keyboard()
    await message.answer(HELP_OVERVIEW, reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("help:"))
async def callback_help(callback: CallbackQuery) -> None:
    """Handle help category selection."""
    data = callback.data.split(":", 1)
    if len(data) < 2:
        await callback.answer()
        return

    key = data[1]

    if key == "back":
        keyboard = build_help_keyboard()
        await callback.message.edit_text(
            HELP_OVERVIEW, reply_markup=keyboard.as_markup()
        )
        await callback.answer()
        return

    cat = HELP_CATEGORIES.get(key)
    if not cat:
        await callback.answer("Unknown category")
        return

    await callback.message.edit_text(
        cat["text"],
        reply_markup=_back_keyboard().as_markup(),
    )
    await callback.answer()


def _back_keyboard() -> InlineKeyboardBuilder:
    """Build a keyboard with just a Back button."""
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Back to categories", callback_data="help:back")
    return builder
