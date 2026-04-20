"""Shop, inventory, and item usage handlers."""

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.evolution import check_evolution, evolve_pokemon, get_possible_evolutions
from telemon.core.items import (
    ALL_ITEMS,
    ITEM_BY_ID,
    ITEM_BY_NAME,
    INCENSE_ID,
    LINKING_CORD_ID,
    RARE_CANDY_ID,
    SOOTHE_BELL_ID,
    XP_BOOST_ID,
)
from telemon.config import BOT_NAME, CURRENCY_SHORT
from telemon.core.constants import MAX_FRIENDSHIP, MAX_LEVEL, MAX_IV_TOTAL
from telemon.core.emoji import item_emoji
from telemon.database.models import InventoryItem, Item, Pokemon, User
from telemon.logging import get_logger

router = Router(name="shop")
logger = get_logger(__name__)

# Friendship gain from /pet
PET_FRIENDSHIP_GAIN = 5
SOOTHE_BELL_MULTIPLIER = 2
PET_COOLDOWN_SECONDS = 1800  # 30 minutes cooldown between /pet uses

# In-memory cooldown tracking for /pet
_pet_cooldowns: dict[int, datetime] = {}
_PET_COOLDOWN_MAX_SIZE = 500

# ──────────────────────────────────────────────
# Shop category data (inline keyboard navigation)
# ──────────────────────────────────────────────

SHOP_CATEGORIES: dict[str, dict] = {
    "evo_stones": {
        "emoji": "🪨",
        "title": "Evo Stones",
        "items": [i for i in ALL_ITEMS if i["category"] == "evolution" and i["id"] <= 10],
    },
    "evo_items": {
        "emoji": "🔗",
        "title": "Evo Items",
        "items": [i for i in ALL_ITEMS if i["category"] == "evolution" and 11 <= i["id"] <= 29],
    },
    "battle": {
        "emoji": "⚔️",
        "title": "Battle",
        "items": [i for i in ALL_ITEMS if i["category"] == "battle"],
    },
    "mega": {
        "emoji": "🌀",
        "title": "Mega Stones",
        "items": [i for i in ALL_ITEMS if i["category"] == "mega_stone"],
    },
    "utility": {
        "emoji": "🧪",
        "title": "Utility",
        "items": [i for i in ALL_ITEMS if i["category"] == "utility"],
    },
    "special": {
        "emoji": "✨",
        "title": "Special",
        "items": [i for i in ALL_ITEMS if i["category"] == "special"],
    },
}

SHOP_CATEGORY_ORDER = ["evo_stones", "evo_items", "battle", "mega", "utility", "special"]

SHOP_OVERVIEW = (
    f"<b>{BOT_NAME} Shop</b>\n\n"
    "Tap a category to browse items.\n\n"
    "<i>Use /buy [id] [qty] to purchase.\n"
    "Use /shopinfo [id] for item details.</i>"
)


def _build_shop_keyboard() -> InlineKeyboardBuilder:
    """Build the shop category selection keyboard."""
    builder = InlineKeyboardBuilder()
    for key in SHOP_CATEGORY_ORDER:
        cat = SHOP_CATEGORIES[key]
        count = len(cat["items"])
        builder.button(
            text=f"{cat['emoji']} {cat['title']} ({count})",
            callback_data=f"shop:{key}",
            style="primary",
        )
    builder.adjust(2)
    return builder


def _shop_back_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Back to shop", callback_data="shop:back")
    return builder


SHOP_PAGE_SIZE = 10  # items per page in category view


def _build_category_text(key: str, page: int = 0) -> str:
    """Build the item list text for a shop category (paginated)."""
    cat = SHOP_CATEGORIES[key]
    items = cat["items"]
    total = len(items)
    total_pages = max(1, (total + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * SHOP_PAGE_SIZE
    end = min(start + SHOP_PAGE_SIZE, total)
    page_items = items[start:end]

    page_label = f" (Page {page + 1}/{total_pages})" if total_pages > 1 else ""
    lines = [f"<b>{cat['emoji']} {cat['title']}{page_label}</b>\n"]
    for item in page_items:
        ie = item_emoji(item["name"])
        lines.append(f"  <code>{item['id']}</code> {ie}{item['name']} — {item['cost']:,} {CURRENCY_SHORT}")
    lines.append(f"\n<i>/buy [id] [qty] to purchase.  /shopinfo [id] for details.</i>")
    return "\n".join(lines)


def _shop_category_keyboard(key: str, page: int = 0) -> InlineKeyboardBuilder:
    """Build keyboard for a category page with pagination + back."""
    cat = SHOP_CATEGORIES[key]
    total = len(cat["items"])
    total_pages = max(1, (total + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)

    builder = InlineKeyboardBuilder()
    if total_pages > 1:
        if page > 0:
            builder.button(text="◀️ Prev", callback_data=f"shop:{key}:{page - 1}")
        if page < total_pages - 1:
            builder.button(text="▶️ Next", callback_data=f"shop:{key}:{page + 1}")
    builder.button(text="◀️ Back to shop", callback_data="shop:back")
    builder.adjust(2)
    return builder


@router.message(Command("shop"))
async def cmd_shop(message: Message) -> None:
    """Handle /shop command."""
    keyboard = _build_shop_keyboard()
    await message.answer(SHOP_OVERVIEW, reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("shop:"))
async def callback_shop(callback: CallbackQuery) -> None:
    """Handle shop category selection and pagination."""
    parts = (callback.data or "").split(":")
    if len(parts) < 2:
        await callback.answer()
        return

    key = parts[1]

    if key == "back":
        keyboard = _build_shop_keyboard()
        await callback.message.edit_text(
            SHOP_OVERVIEW, reply_markup=keyboard.as_markup()
        )
        await callback.answer()
        return

    cat = SHOP_CATEGORIES.get(key)
    if not cat:
        await callback.answer("Unknown category")
        return

    # Parse optional page number (shop:mega:2)
    page = 0
    if len(parts) >= 3 and parts[2].isdigit():
        page = int(parts[2])

    text = _build_category_text(key, page)
    kb = _shop_category_keyboard(key, page)
    await callback.message.edit_text(
        text, reply_markup=kb.as_markup()
    )
    await callback.answer()


@router.message(Command("shopinfo", "iteminfo"))
async def cmd_shopinfo(message: Message) -> None:
    """Show detailed info about a shop item."""
    text = message.text or ""
    args = text.split()

    if len(args) < 2:
        await message.answer("Usage: /shopinfo [item_id]\nExample: /shopinfo 29")
        return

    try:
        item_id = int(args[1])
    except ValueError:
        # Try by name
        name = " ".join(args[1:]).lower()
        item_data = ITEM_BY_NAME.get(name)
        if not item_data:
            await message.answer("Item not found! Use /shop to see item IDs.")
            return
        item_id = item_data["id"]

    item_data = ITEM_BY_ID.get(item_id)
    if not item_data:
        await message.answer("Item not found! Use /shop to see item IDs.")
        return

    desc = item_data.get("description", "No description available.")
    props = []
    if item_data.get("is_consumable"):
        props.append("Consumable")
    if item_data.get("is_holdable"):
        props.append("Holdable")

    await message.answer(
        f"<b>{item_data['name']}</b> (ID: {item_data['id']})\n\n"
        f"{desc}\n\n"
        f"<b>Category:</b> {item_data['category'].title()}\n"
        f"<b>Cost:</b> {item_data['cost']:,} {CURRENCY_SHORT}\n"
        f"<b>Sell:</b> {item_data['sell_price']:,} {CURRENCY_SHORT}\n"
        f"<b>Properties:</b> {', '.join(props) if props else 'None'}"
    )


@router.message(Command("buy"))
async def cmd_buy(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /buy command - buy items by ID."""
    if not message.text:
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "Please specify an item ID to buy!\n"
            "Usage: /buy [item_id] [quantity]\n"
            "Example: /buy 201 5 (buy 5 Rare Candies)\n\n"
            "Use /shop to see item IDs."
        )
        return

    # Parse item ID
    try:
        item_id = int(args[1])
    except ValueError:
        await message.answer(
            "Invalid item ID! Use a number.\n"
            "Example: /buy 201 5\n\n"
            "Use /shop to see item IDs."
        )
        return

    # Parse quantity (default 1)
    quantity = 1
    if len(args) >= 3:
        try:
            quantity = int(args[2])
            if quantity < 1:
                await message.answer("Quantity must be at least 1!")
                return
            if quantity > 99:
                await message.answer("Maximum quantity per purchase is 99!")
                return
        except ValueError:
            await message.answer("Invalid quantity! Use a number.")
            return

    # Get the item from database
    result = await session.execute(
        select(Item).where(Item.id == item_id).where(Item.is_purchasable == True)
    )
    item = result.scalar_one_or_none()

    if not item:
        await message.answer(
            f"Item with ID {item_id} not found in the shop!\n"
            "Use /shop to see available items."
        )
        return

    total_cost = item.cost * quantity

    # Atomic balance deduction — prevents race conditions where two concurrent
    # /buy commands both see sufficient balance and double-spend.
    result = await session.execute(
        update(User)
        .where(User.telegram_id == user.telegram_id)
        .where(User.balance >= total_cost)
        .values(balance=User.balance - total_cost)
    )

    if result.rowcount == 0:
        await message.answer(
            f"Not enough {CURRENCY_SHORT}!\n\n"
            f"Item: {item.name} (ID: {item.id})\n"
            f"Price: {item.cost:,} {CURRENCY_SHORT} x {quantity} = {total_cost:,} {CURRENCY_SHORT}\n"
            f"Your balance: {user.balance:,} {CURRENCY_SHORT}"
        )
        return

    # Refresh the user object so balance is up-to-date for the response
    await session.refresh(user)

    # Add to inventory (atomic upsert)
    inv_result = await session.execute(
        select(InventoryItem)
        .where(InventoryItem.user_id == user.telegram_id)
        .where(InventoryItem.item_id == item_id)
    )
    inventory_item = inv_result.scalar_one_or_none()

    if inventory_item:
        inventory_item.quantity += quantity
    else:
        inventory_item = InventoryItem(
            user_id=user.telegram_id,
            item_id=item_id,
            quantity=quantity,
        )
        session.add(inventory_item)

    await session.commit()

    logger.info(
        "User purchased item",
        user_id=user.telegram_id,
        item_id=item_id,
        item_name=item.name,
        quantity=quantity,
        cost=total_cost,
    )

    await message.answer(
        f"<b>Purchase Successful!</b>\n\n"
        f"Bought: {item.name} x{quantity}\n"
        f"Cost: {total_cost:,} {CURRENCY_SHORT}\n"
        f"Remaining balance: {user.balance:,} {CURRENCY_SHORT}\n\n"
        f"<i>Use /inventory to see your items.</i>"
    )


@router.message(Command("inventory", "bag"))
async def cmd_inventory(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /inventory command."""
    # Get user's inventory with item details
    result = await session.execute(
        select(InventoryItem)
        .where(InventoryItem.user_id == user.telegram_id)
        .where(InventoryItem.quantity > 0)
    )
    inventory_items = result.scalars().all()

    if not inventory_items:
        await message.answer(
            "<b>Your Inventory</b>\n\n"
            "You don't have any items yet!\n"
            "Visit /shop to purchase items."
        )
        return

    # Group items by category
    categories: dict[str, list[tuple[int, str, int]]] = {}

    for inv_item in inventory_items:
        # Get item details
        item_result = await session.execute(
            select(Item).where(Item.id == inv_item.item_id)
        )
        item = item_result.scalar_one_or_none()

        if item:
            category = item.category.title() if item.category else "Other"
            if category not in categories:
                categories[category] = []
            categories[category].append((item.id, item.name, inv_item.quantity))

    # Build message
    lines = ["<b>Your Inventory</b>\n"]

    # Display order with clean names
    category_display = [
        ("Evolution", "Evolution"),
        ("Battle", "Battle"),
        ("Mega_Stone", "Mega Stone"),
        ("Utility", "Utility"),
        ("Special", "Special"),
        ("Other", "Other"),
    ]
    for cat_key, cat_label in category_display:
        if cat_key in categories:
            lines.append(f"\n<b>{cat_label} Items</b>")
            for item_id, item_name, qty in categories[cat_key]:
                ie = item_emoji(item_name)
                lines.append(f"  <code>{item_id}</code> {ie}{item_name} x{qty}")

    lines.append("\n<i>Use /use [item_id] [qty|max] [pokemon#] to use an item.\nUse /sell [item_id] [qty|max] to sell items.</i>")

    await message.answer("\n".join(lines))


def _parse_quantity(arg: str, inventory_qty: int) -> int | None:
    """Parse a quantity argument: int, 'max', or 'all'.

    Returns the resolved quantity (capped at *inventory_qty*),
    or ``None`` if the string is not a valid quantity token.
    """
    if arg.lower() in ("max", "all"):
        return inventory_qty
    try:
        val = int(arg)
        if val < 1:
            return None
        return min(val, inventory_qty)
    except ValueError:
        return None


@router.message(Command("use"))
async def cmd_use(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /use command for using items by ID.

    Syntax:
        /use <item_id>                      — use 1 on selected Pokemon
        /use <item_id> <qty|max>            — use qty on selected Pokemon
        /use <item_id> <qty|max> <pokemon#> — use qty on Pokemon #N
    """
    if not message.text:
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "Please specify an item ID to use!\n"
            "Usage: /use [item_id] [qty|max] [pokemon#]\n\n"
            "<b>Examples:</b>\n"
            "/use 201 — use 1 Rare Candy on selected Pokemon\n"
            "/use 201 20 — use 20 Rare Candies on selected Pokemon\n"
            "/use 201 20 3 — use 20 Rare Candies on Pokemon #3\n"
            "/use 201 max — use as many as possible\n"
            "/use 29 — use Linking Cord on selected Pokemon"
        )
        return

    # Parse item ID
    try:
        item_id = int(args[1])
    except ValueError:
        await message.answer(
            "Invalid item ID! Use a number.\n"
            "Use /inventory to see your items."
        )
        return

    # Check if user has this item
    inv_result = await session.execute(
        select(InventoryItem)
        .where(InventoryItem.user_id == user.telegram_id)
        .where(InventoryItem.item_id == item_id)
        .where(InventoryItem.quantity > 0)
    )
    inventory_item = inv_result.scalar_one_or_none()

    if not inventory_item:
        await message.answer(
            f"You don't have item ID {item_id}!\n"
            "Use /inventory to see your items."
        )
        return

    # Parse optional quantity (arg 2) and pokemon index (arg 3)
    # Format: /use <id> [qty|max] [pokemon#]
    use_qty = 1
    pokemon_idx = None

    if len(args) >= 3:
        parsed = _parse_quantity(args[2], inventory_item.quantity)
        if parsed is not None:
            use_qty = parsed
        else:
            await message.answer(
                "Invalid quantity! Use a number or 'max'.\n"
                "Example: /use 201 20  or  /use 201 max"
            )
            return

    if len(args) >= 4:
        pokemon_idx = args[3]  # Pass as string; _resolve_use_target handles l/latest/0

    # Get item details
    item_result = await session.execute(
        select(Item).where(Item.id == item_id)
    )
    item = item_result.scalar_one_or_none()

    if not item:
        await message.answer("Item not found!")
        return

    category = item.category.lower() if item.category else ""

    # ── Evolution items: direct use triggers evolution ──
    if category == "evolution":
        # Get the target Pokemon
        poke = await _resolve_use_target(session, user, pokemon_idx)
        if poke is None:
            await message.answer(
                f"Please specify which Pokemon to use {item.name} on!\n"
                f"Usage: /use {item_id} 1 [pokemon#]\n"
                f"Or select a Pokemon first with /select [number]"
            )
            return

        # Is this a Linking Cord?
        if item_id == LINKING_CORD_ID:
            # Try trade evolution
            success, msg = await evolve_pokemon(
                session, poke, user.telegram_id, use_item="linking cord"
            )
            if success:
                await session.refresh(poke)
                await message.answer(
                    f"<b>Linking Cord Used!</b>\n\n"
                    f"{msg}\n\n"
                    f"Your Pokemon is now a <b>{poke.species.name}</b>!"
                )
            else:
                await message.answer(
                    f"Cannot use Linking Cord on {poke.display_name}.\n{msg}"
                )
            return

        # Regular evolution item
        success, msg = await evolve_pokemon(
            session, poke, user.telegram_id, use_item=item.name
        )
        if success:
            await session.refresh(poke)
            await message.answer(
                f"<b>{item.name} Used!</b>\n\n"
                f"{msg}\n\n"
                f"Your Pokemon is now a <b>{poke.species.name}</b>!"
            )
        else:
            await message.answer(
                f"Cannot use {item.name} on {poke.display_name}.\n{msg}"
            )
        return

    # ── Rare Candy (supports multi-use) ──
    if item_id == RARE_CANDY_ID:
        poke = await _resolve_use_target(session, user, pokemon_idx)
        if poke is None:
            await message.answer(
                "Please specify which Pokemon to use Rare Candy on!\n"
                "Usage: /use 201 [qty|max] [pokemon#]\n\n"
                "<b>Examples:</b>\n"
                "/use 201 20 — use 20 on selected Pokemon\n"
                "/use 201 20 3 — use 20 on Pokemon #3\n"
                "/use 201 max — use as many as possible"
            )
            return

        if poke.level >= MAX_LEVEL:
            await message.answer(f"{poke.display_name} is already at max level ({MAX_LEVEL})!")
            return

        # Calculate how many can actually be used
        levels_available = MAX_LEVEL - poke.level  # room to grow
        if levels_available <= 0:
            await message.answer(f"{poke.display_name} is already at max level ({MAX_LEVEL})!")
            return

        max_usable = min(inventory_item.quantity, levels_available)
        amount = min(use_qty, max_usable)

        if amount <= 0:
            await message.answer(f"{poke.display_name} is already at max level ({MAX_LEVEL})!")
            return

        old_level = poke.level
        poke.level += amount
        poke.friendship = min(MAX_FRIENDSHIP, poke.friendship + 3 * amount)
        inventory_item.quantity -= amount
        await session.commit()

        lines = [
            f"<b>Rare Candy ×{amount} Used!</b>\n",
            f"{poke.display_name} grew from Lv.{old_level} → Lv.{poke.level}!",
        ]
        if poke.level >= MAX_LEVEL:
            lines.append(f"\n🎉 {poke.display_name} reached the max level!")
        lines.append(f"\n<i>Rare Candies remaining: {inventory_item.quantity}</i>")

        await message.answer("\n".join(lines))

        logger.info(
            "User used rare candy",
            user_id=user.telegram_id,
            pokemon=poke.species.name,
            amount=amount,
            old_level=old_level,
            new_level=poke.level,
        )

        # Update quest progress for item usage
        from telemon.core.quests import update_quest_progress
        await update_quest_progress(session, user.telegram_id, "use_item")
        await session.commit()

        # Check if can evolve now
        evo_result = await check_evolution(session, poke, user.telegram_id)
        if evo_result.can_evolve and evo_result.trigger == "level":
            await message.answer(
                f"✨ {poke.display_name} is ready to evolve! Use /evolve to evolve it."
            )
        return

    # ── Soothe Bell ──
    if item_id == SOOTHE_BELL_ID:
        poke = await _resolve_use_target(session, user, pokemon_idx)
        if poke is None:
            await message.answer(
                "Please specify which Pokemon to give the Soothe Bell to!\n"
                "Usage: /use 30 1 [pokemon#]"
            )
            return

        poke.held_item = "Soothe Bell"
        await session.commit()

        await message.answer(
            f"<b>Soothe Bell</b>\n\n"
            f"{poke.display_name} is now holding a Soothe Bell!\n"
            f"Friendship gains are doubled while holding this item.\n\n"
            f"Current friendship: {poke.friendship}/{MAX_FRIENDSHIP}"
        )
        return

    # ── Incense ──
    if item_id == INCENSE_ID:
        from telemon.config import settings

        # Runtime-configurable spawn count
        from telemon.bot.handlers.admin import get_runtime_config
        spawn_count = get_runtime_config("incense_count", settings.incense_spawn_count)

        is_group = message.chat.type in ("group", "supergroup")

        if is_group:
            # Only group admins can use incense in groups
            try:
                member = await message.bot.get_chat_member(
                    message.chat.id, user.telegram_id
                )
                is_admin = member.status in ("administrator", "creator")
            except Exception:
                is_admin = False

            if not is_admin:
                await message.answer(
                    "🔒 Only group admins can use Incense in group chats!\n"
                    "Use it in my DMs instead — I'll spawn Pokémon just for you."
                )
                return

            # Check if group already has active incense
            from telemon.database.models import Group
            group_result = await session.execute(
                select(Group).where(Group.chat_id == message.chat.id)
            )
            group = group_result.scalar_one_or_none()
            if group and group.incense_spawns_remaining > 0:
                await message.answer(
                    f"🕐 Group Incense is already active! ({group.incense_spawns_remaining} spawns remaining)"
                )
                return

            # Activate group incense
            if group:
                group.incense_spawns_remaining = spawn_count
                group.incense_activated_by = user.telegram_id
            inventory_item.quantity -= 1
            await session.commit()

            await message.answer(
                "🟢 <b>Group Incense Activated!</b>\n\n"
                f"Activated by {user.display_name}\n"
                f"<b>{spawn_count}</b> Pokémon will spawn every 10 seconds!\n\n"
                f"<i>Incense remaining: {inventory_item.quantity}</i>"
            )
        else:
            # DM mode — check if already active
            if user.incense_spawns_remaining > 0:
                await message.answer(
                    f"🕐 Your Incense is already active! ({user.incense_spawns_remaining} spawns remaining)"
                )
                return

            # Activate personal incense
            user.incense_spawns_remaining = spawn_count
            inventory_item.quantity -= 1
            await session.commit()

            await message.answer(
                "🟢 <b>Incense Activated!</b>\n\n"
                f"<b>{spawn_count}</b> Pokémon will spawn in your DMs every 10 seconds!\n"
                "Stay ready with /catch when they appear.\n\n"
                f"<i>Incense remaining: {inventory_item.quantity}</i>"
            )

        logger.info(
            "Incense activated",
            user_id=user.telegram_id,
            mode="group" if is_group else "dm",
            chat_id=message.chat.id,
            spawn_count=spawn_count,
        )
        return

    # ── XP Boost ──
    if item_id == XP_BOOST_ID:
        from datetime import timedelta

        now = datetime.utcnow()

        # Check if already active
        if user.xp_boost_until and user.xp_boost_until > now:
            remaining = int((user.xp_boost_until - now).total_seconds() / 60)
            await message.answer(
                f"🕐 Your XP Boost is already active! ({remaining} min remaining)"
            )
            return

        # Activate XP boost
        user.xp_boost_until = now + timedelta(hours=1)
        inventory_item.quantity -= 1
        await session.commit()

        await message.answer(
            "🟢 <b>XP Boost Activated!</b>\n\n"
            "You'll earn <b>2× XP</b> from catches and battles for 1 hour!\n\n"
            f"<i>XP Boosts remaining: {inventory_item.quantity}</i>"
        )

        logger.info("XP Boost activated", user_id=user.telegram_id)
        return

    # ── Battle items ──
    if category == "battle":
        poke = await _resolve_use_target(session, user, pokemon_idx)
        if poke is None:
            await message.answer(
                f"Please specify which Pokemon to give {item.name} to!\n"
                f"Usage: /use {item_id} 1 [pokemon#]"
            )
            return

        poke.held_item = item.name
        await session.commit()

        await message.answer(
            f"<b>{item.name} Equipped!</b>\n\n"
            f"{poke.display_name} is now holding {item.name}."
        )
        return

    # ── Mega stones ──
    if category == "mega_stone":
        poke = await _resolve_use_target(session, user, pokemon_idx)
        if poke is None:
            await message.answer(
                f"Please specify which Pokemon to give {item.name} to!\n"
                f"Usage: /use {item_id} [pokemon#]"
            )
            return

        # Check if the Pokemon can actually mega evolve with this stone
        from telemon.core.forms import can_mega_evolve
        mega = can_mega_evolve(poke.species_id, item.name_lower)
        warning = ""
        if not mega:
            warning = (
                f"\n\n<i>Note: {poke.display_name} cannot mega evolve "
                f"with this stone. It may work on a different Pokemon.</i>"
            )

        poke.held_item = item.name
        await session.commit()

        await message.answer(
            f"<b>{item.name} Equipped!</b>\n\n"
            f"{poke.display_name} is now holding {item.name}.{warning}"
        )
        return

    await message.answer(
        f"Cannot use {item.name} directly.\n"
        f"Check /help for how to use this item."
    )


@router.message(Command("sell"))
async def cmd_sell(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /sell command — sell items from inventory.

    Syntax:
        /sell <item_id>           — sell 1
        /sell <item_id> <qty>     — sell qty
        /sell <item_id> max       — sell all
    """
    if not message.text:
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "Please specify an item ID to sell!\n"
            "Usage: /sell [item_id] [qty|max]\n\n"
            "<b>Examples:</b>\n"
            "/sell 201 — sell 1 Rare Candy\n"
            "/sell 201 10 — sell 10 Rare Candies\n"
            "/sell 201 max — sell all Rare Candies\n\n"
            "<i>Use /inventory to see your items.</i>"
        )
        return

    # Parse item ID
    try:
        item_id = int(args[1])
    except ValueError:
        await message.answer(
            "Invalid item ID! Use a number.\n"
            "Use /inventory to see your items."
        )
        return

    # Check inventory
    inv_result = await session.execute(
        select(InventoryItem)
        .where(InventoryItem.user_id == user.telegram_id)
        .where(InventoryItem.item_id == item_id)
        .where(InventoryItem.quantity > 0)
    )
    inventory_item = inv_result.scalar_one_or_none()

    if not inventory_item:
        await message.answer(
            f"You don't have item ID {item_id}!\n"
            "Use /inventory to see your items."
        )
        return

    # Get item details for sell price
    item_result = await session.execute(
        select(Item).where(Item.id == item_id)
    )
    item = item_result.scalar_one_or_none()

    if not item:
        await message.answer("Item not found!")
        return

    if item.sell_price <= 0:
        await message.answer(f"{item.name} cannot be sold!")
        return

    # Parse quantity
    sell_qty = 1
    if len(args) >= 3:
        parsed = _parse_quantity(args[2], inventory_item.quantity)
        if parsed is None:
            await message.answer(
                "Invalid quantity! Use a number or 'max'.\n"
                "Example: /sell 201 10  or  /sell 201 max"
            )
            return
        sell_qty = parsed

    sell_qty = min(sell_qty, inventory_item.quantity)
    if sell_qty <= 0:
        await message.answer("Nothing to sell!")
        return

    total_earnings = item.sell_price * sell_qty

    # Atomic inventory deduction — prevents double-selling race conditions
    inv_update = await session.execute(
        update(InventoryItem)
        .where(InventoryItem.user_id == user.telegram_id)
        .where(InventoryItem.item_id == item_id)
        .where(InventoryItem.quantity >= sell_qty)
        .values(quantity=InventoryItem.quantity - sell_qty)
    )

    if inv_update.rowcount == 0:
        await message.answer("Not enough items to sell (concurrent operation detected).")
        return

    # Atomic balance credit
    await session.execute(
        update(User)
        .where(User.telegram_id == user.telegram_id)
        .values(balance=User.balance + total_earnings)
    )

    await session.commit()

    # Refresh for accurate response values
    await session.refresh(user)
    await session.refresh(inventory_item)

    logger.info(
        "User sold item",
        user_id=user.telegram_id,
        item_id=item_id,
        item_name=item.name,
        quantity=sell_qty,
        earnings=total_earnings,
    )

    await message.answer(
        f"<b>Sold {item.name} ×{sell_qty}!</b>\n\n"
        f"Earned: {total_earnings:,} {CURRENCY_SHORT}\n"
        f"New balance: {user.balance:,} {CURRENCY_SHORT}\n\n"
        f"<i>{item.name} remaining: {inventory_item.quantity}</i>"
    )


@router.message(Command("pet"))
async def cmd_pet(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /pet command to increase friendship of selected Pokemon."""
    # Check cooldown
    now = datetime.utcnow()
    if user.telegram_id in _pet_cooldowns:
        elapsed = (now - _pet_cooldowns[user.telegram_id]).total_seconds()
        if elapsed < PET_COOLDOWN_SECONDS:
            remaining = int(PET_COOLDOWN_SECONDS - elapsed)
            mins, secs = divmod(remaining, 60)
            await message.answer(f"You can pet again in {mins}m {secs}s!")
            return

    _pet_cooldowns[user.telegram_id] = now

    # Prune expired cooldown entries to prevent memory leaks
    if len(_pet_cooldowns) > _PET_COOLDOWN_MAX_SIZE:
        from datetime import timedelta
        cutoff = now - timedelta(seconds=PET_COOLDOWN_SECONDS)
        expired = [uid for uid, ts in _pet_cooldowns.items() if ts < cutoff]
        for uid in expired:
            del _pet_cooldowns[uid]

    text = message.text or ""
    args = text.split()

    arg = args[1] if len(args) >= 2 else None
    poke = await _resolve_use_target(session, user, arg)

    if not poke:
        await message.answer(
            "No Pokemon selected!\n"
            "Usage: /pet [pokemon#] or /pet (uses selected Pokemon)"
        )
        return

    if poke.friendship >= MAX_FRIENDSHIP:
        await message.answer(
            f"{poke.display_name} already has maximum friendship! ({MAX_FRIENDSHIP}/{MAX_FRIENDSHIP})\n"
            f"❤️ Your bond couldn't be stronger!"
        )
        return

    # Calculate friendship gain
    gain = PET_FRIENDSHIP_GAIN
    has_soothe_bell = poke.held_item and poke.held_item.lower() == "soothe bell"
    if has_soothe_bell:
        gain *= SOOTHE_BELL_MULTIPLIER

    old_friendship = poke.friendship
    poke.friendship = min(MAX_FRIENDSHIP, poke.friendship + gain)
    actual_gain = poke.friendship - old_friendship
    await session.commit()

    bell_text = " (Soothe Bell bonus!)" if has_soothe_bell else ""
    hearts = "❤️" * min(5, poke.friendship // 50)

    response = (
        f"You pet <b>{poke.display_name}</b>!\n"
        f"Friendship: {poke.friendship}/{MAX_FRIENDSHIP} (+{actual_gain}{bell_text})\n"
        f"{hearts}"
    )

    # Update quest progress
    from telemon.core.quests import update_quest_progress

    completed = await update_quest_progress(session, user.telegram_id, "pet")
    if completed:
        await session.commit()
        for q in completed:
            response += f"\n📋 Quest complete: {q.description} (+{q.reward_coins:,} {CURRENCY_SHORT})"

    # Check if can evolve with friendship now
    evo_result = await check_evolution(session, poke, user.telegram_id)
    if evo_result.can_evolve and evo_result.trigger == "friendship":
        response += f"\n\n{poke.display_name} is ready to evolve! Use /evolve to evolve it."

    await message.answer(response)


async def _resolve_use_target(
    session: AsyncSession, user: User, pokemon_idx: int | str | None
) -> Pokemon | None:
    """Resolve a Pokemon target by index, 'l'/'latest' for latest, '0' for selected."""
    LATEST_ALIASES = {"l", "-l", "--latest", "-latest", "latest"}

    if pokemon_idx is not None:
        # Normalize string to int or alias
        if isinstance(pokemon_idx, str):
            if pokemon_idx.lower() in LATEST_ALIASES:
                result = await session.execute(
                    select(Pokemon)
                    .where(Pokemon.owner_id == user.telegram_id)
                    .order_by(Pokemon.caught_at.desc())
                    .limit(1)
                )
                return result.scalar_one_or_none()
            elif pokemon_idx == "0":
                pass  # Fall through to selected Pokemon
            elif pokemon_idx.isdigit():
                pokemon_idx = int(pokemon_idx)
            else:
                return None

        if isinstance(pokemon_idx, int) and pokemon_idx > 0:
            poke_result = await session.execute(
                select(Pokemon)
                .where(Pokemon.owner_id == user.telegram_id)
                .order_by(Pokemon.caught_at.asc())
            )
            pokemon_list = list(poke_result.scalars().all())

            if pokemon_idx < 1 or pokemon_idx > len(pokemon_list):
                return None
            return pokemon_list[pokemon_idx - 1]

    # Use selected Pokemon
    if user.selected_pokemon_id:
        sel_result = await session.execute(
            select(Pokemon)
            .where(Pokemon.id == user.selected_pokemon_id)
            .where(Pokemon.owner_id == user.telegram_id)
        )
        return sel_result.scalar_one_or_none()

    return None
