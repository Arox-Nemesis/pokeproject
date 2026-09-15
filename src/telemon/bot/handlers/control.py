"""Owner-only, persistent default and per-group game setting controls."""

from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.bot.handlers.admin import _runtime_overrides
from telemon.config import settings
from telemon.core.text import esc
from telemon.database.models import BotConfig, Group

router = Router(name="control")


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    default: int | float
    minimum: int | float
    maximum: int | float
    integer: bool = True


SETTING_SPECS = (
    SettingSpec(
        "spawn_threshold_min", "Spawn threshold minimum", settings.spawn_threshold_min, 1, 1000
    ),
    SettingSpec(
        "spawn_threshold_max", "Spawn threshold maximum", settings.spawn_threshold_max, 1, 1000
    ),
    SettingSpec(
        "spawn_time_min_minutes",
        "Spawn time minimum (minutes)",
        settings.spawn_time_min_minutes,
        1,
        1440,
    ),
    SettingSpec(
        "spawn_time_max_minutes",
        "Spawn time maximum (minutes)",
        settings.spawn_time_max_minutes,
        1,
        1440,
    ),
    SettingSpec(
        "spawn_timeout_seconds",
        "Spawn timeout (seconds)",
        settings.spawn_timeout_seconds,
        30,
        86400,
    ),
    SettingSpec(
        "spawn_min_message_length",
        "Minimum message length",
        settings.spawn_min_message_length,
        1,
        4096,
    ),
    SettingSpec(
        "spawn_user_cooldown_seconds",
        "User cooldown (seconds)",
        settings.spawn_user_cooldown_seconds,
        0,
        3600,
        False,
    ),
    SettingSpec(
        "spawn_guild_cooldown_seconds",
        "Group cooldown (seconds)",
        settings.spawn_guild_cooldown_seconds,
        0,
        3600,
        False,
    ),
    SettingSpec("daily_reward_base", "Daily reward base", settings.daily_reward_base, 1, 1_000_000),
    SettingSpec(
        "daily_streak_bonus", "Daily streak bonus", settings.daily_streak_bonus, 0, 1_000_000
    ),
    SettingSpec("daily_streak_max", "Daily streak maximum", settings.daily_streak_max, 1, 3650),
    SettingSpec(
        "catch_reward_min", "Catch reward minimum", settings.catch_reward_min, 0, 1_000_000
    ),
    SettingSpec(
        "catch_reward_max", "Catch reward maximum", settings.catch_reward_max, 1, 1_000_000
    ),
    SettingSpec("market_fee_percent", "Market fee (%)", settings.market_fee_percent, 0, 50),
    SettingSpec("incense_spawn_count", "Incense spawn count", settings.incense_spawn_count, 1, 500),
    SettingSpec(
        "incense_spawn_interval_seconds",
        "Incense interval (seconds)",
        settings.incense_spawn_interval_seconds,
        3,
        3600,
    ),
    SettingSpec("incense_max_uses_12h", "Incense uses per user / 12h", 1, 1, 100),
    SettingSpec("incense_window_hours", "Incense cooldown window (hours)", 12, 1, 168),
)
_SPEC_BY_KEY = {spec.key: spec for spec in SETTING_SPECS}
# Owner input state is intentionally tiny and ephemeral; stored values are in PostgreSQL.
_pending_input: dict[int, tuple[str, str | int]] = {}


def _is_owner(user_id: int | None) -> bool:
    return user_id is not None and settings.owner_id is not None and user_id == settings.owner_id


def _may_manage_here(message: Message) -> bool:
    return (
        _is_owner(message.from_user.id if message.from_user else None)
        and message.chat.type != "private"
        and (settings.owner_group_id is None or message.chat.id == settings.owner_group_id)
    )


async def _get_group(session: AsyncSession, chat_id: int, title: str | None = None) -> Group:
    result = await session.execute(select(Group).where(Group.chat_id == chat_id))
    group = result.scalar_one_or_none()
    if group is None:
        group = Group(chat_id=chat_id, title=title)
        session.add(group)
        await session.flush()
    return group


async def _default_values(session: AsyncSession) -> dict[str, int | float]:
    rows = (
        await session.execute(select(BotConfig).where(BotConfig.key.like("default.%")))
    ).scalars()
    values = {spec.key: spec.default for spec in SETTING_SPECS}
    for row in rows:
        key = row.key.removeprefix("default.")
        spec = _SPEC_BY_KEY.get(key)
        if spec is None:
            continue
        try:
            values[key] = int(row.value) if spec.integer else float(row.value)
        except ValueError:
            continue
    return values


def _group_values(group: Group, defaults: dict[str, int | float]) -> dict[str, int | float]:
    overrides = group.settings or {}
    return {key: overrides.get(key, value) for key, value in defaults.items()}


def _panel_keyboard(groups: list[Group]) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Default", callback_data="control:default")
    for number, group in enumerate(groups, 1):
        builder.button(text=f"Group {number:02d}", callback_data=f"control:group:{group.chat_id}")
    builder.adjust(2)
    return builder


def _settings_keyboard(scope: str | int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for spec in SETTING_SPECS:
        builder.button(text=spec.label, callback_data=f"control:edit:{scope}:{spec.key}")
    builder.button(text="← Control panel", callback_data="control:home")
    builder.adjust(1)
    return builder


async def _show_panel(message: Message | CallbackQuery, session: AsyncSession) -> None:
    groups = (
        (await session.execute(select(Group).order_by(Group.created_at, Group.chat_id)))
        .scalars()
        .all()
    )
    text = (
        "<b>Owner Control Panel</b>\n\n"
        "<b>Default</b> changes the baseline used by groups without an override.\n"
        "Select an existing bot group to change only that group's settings.\n"
        "This panel is available only in OWNER_GROUP_ID."
    )
    markup = _panel_keyboard(groups).as_markup()
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=markup)
        await message.answer()
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Command("control"))
async def cmd_control(message: Message, session: AsyncSession) -> None:
    """Open owner control UI or add a group directly with /control add <chat_id>."""
    if not _may_manage_here(message):
        await message.answer("This owner control is only available in the configured owner group.")
        return
    parts = (message.text or "").split()
    await _show_panel(message, session)


@router.callback_query(F.data.startswith("control:"))
async def control_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    if settings.owner_group_id is not None and callback.message.chat.id != settings.owner_group_id:
        await callback.answer("Use the configured owner group.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1]
    if action == "home":
        await _show_panel(callback, session)
    elif action in {"default", "group"}:
        scope: str | int = "default" if action == "default" else int(parts[2])
        defaults = await _default_values(session)
        if scope == "default":
            values = defaults
            heading = "<b>Default settings</b>"
        else:
            group = await _get_group(session, scope)
            values = _group_values(group, defaults)
            heading = f"<b>Group settings</b> <code>{scope}</code>"
        lines = [heading, "", "Tap a setting to change it:"]
        lines.extend(f"• {spec.label}: <b>{values[spec.key]}</b>" for spec in SETTING_SPECS)
        await callback.message.edit_text(
            "\n".join(lines), reply_markup=_settings_keyboard(scope).as_markup()
        )
        await callback.answer()
    elif action == "edit" and len(parts) == 4:
        scope = parts[2] if parts[2] == "default" else int(parts[2])
        spec = _SPEC_BY_KEY.get(parts[3])
        if spec is None:
            await callback.answer("Unknown setting.", show_alert=True)
            return
        _pending_input[callback.from_user.id] = ("edit", f"{scope}:{spec.key}")
        await callback.message.answer(
            f"Send a value for <b>{esc(spec.label)}</b> ({spec.minimum}–{spec.maximum})."
        )
        await callback.answer()



@router.message(F.text & ~F.text.startswith("/"))
async def control_input(message: Message, session: AsyncSession) -> None:
    """Consume the next owner message only while a control-panel input is pending."""
    if not _may_manage_here(message) or not message.from_user:
        return
    pending = _pending_input.pop(message.from_user.id, None)
    if pending is None:
        return
    mode, detail = pending
    value = (message.text or "").strip()
    scope_text, key = str(detail).split(":", 1)
    spec = _SPEC_BY_KEY[key]
    try:
        parsed: int | float = int(value) if spec.integer else float(value)
    except ValueError:
        await message.answer("That value is not valid. Re-open the setting and try again.")
        return
    if not spec.minimum <= parsed <= spec.maximum:
        await message.answer(f"Value must be between {spec.minimum} and {spec.maximum}.")
        return

    # Keep paired minimum/maximum settings valid so spawn scheduling can never
    # reach an invalid random range after a panel edit.
    defaults = await _default_values(session)
    current_values = defaults
    if scope_text != "default":
        current_values = _group_values(await _get_group(session, int(scope_text)), defaults)
    paired_key = {
        "spawn_threshold_min": "spawn_threshold_max",
        "spawn_threshold_max": "spawn_threshold_min",
        "spawn_time_min_minutes": "spawn_time_max_minutes",
        "spawn_time_max_minutes": "spawn_time_min_minutes",
        "catch_reward_min": "catch_reward_max",
        "catch_reward_max": "catch_reward_min",
    }.get(key)
    if paired_key:
        paired_value = current_values[paired_key]
        invalid_pair = key.endswith("_min") and parsed > paired_value
        invalid_pair = invalid_pair or (key.endswith("_max") and parsed < paired_value)
        if invalid_pair:
            await message.answer(f"This value conflicts with {paired_key} ({paired_value}).")
            return

    if scope_text == "default":
        config_key = f"default.{key}"
        row = (
            await session.execute(select(BotConfig).where(BotConfig.key == config_key))
        ).scalar_one_or_none()
        if row is None:
            session.add(BotConfig(key=config_key, value=str(parsed)))
        else:
            row.value = str(parsed)
        _runtime_overrides[config_key] = parsed
    else:
        group = await _get_group(session, int(scope_text))
        overrides = dict(group.settings or {})
        overrides[key] = parsed
        group.settings = overrides
    await session.commit()
    await message.answer(
        f"✅ Saved <b>{esc(spec.label)}</b> = <b>{parsed}</b> for {esc(scope_text)}."
    )
