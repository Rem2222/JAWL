import asyncio
import json
import os
from pathlib import Path

from aiogram import Dispatcher, F
from aiogram.types import (
    Message, BotCommand, BotCommandScopeAllPrivateChats,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)

from src.utils.event.bus import EventBus
from src.utils.event.registry import Events
from src.utils.logger import system_logger

from src.l0_state.interfaces.state import AiogramState
from src.l0_state.agent.state import AgentState, AgentStatus
from src.l2_interfaces.telegram.aiogram.client import AiogramClient


class AiogramEvents:
    """
    Слушатель событий Aiogram (Bot API).
    Обновляет AiogramState и публикует события в EventBus.
    """

    def __init__(
        self,
        aiogram_client: AiogramClient,
        state: AiogramState,
        event_bus: EventBus,
        agent_state: AgentState,
        config_path: str | None = None,
    ):
        self.client = aiogram_client
        self.state = state
        self.bus = event_bus
        self.agent_state = agent_state
        self._config_path = config_path or os.path.join(os.getcwd(), "config", "models.json")

        self.dp = Dispatcher()
        self._polling_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Регистрирует роутеры и запускает фоновый поллинг."""

        if self._polling_task:
            return

        bot = self.client.bot()

        # Регистрация команд
        self.dp.message.register(self._cmd_restart, F.text == "/restart")
        self.dp.message.register(self._cmd_status, F.text == "/status")
        self.dp.message.register(self._cmd_models, F.text == "/models")
        self.dp.message.register(self._cmd_stop, F.text == "/stop")
        self.dp.message.register(self._cmd_help, F.text == "/help")

        # Регистрация callback-обработчиков для inline-кнопок
        # Важно: более специфичные фильтры регистрируются ДО общих
        self.dp.callback_query.register(self._cb_back, F.data == "prov:back")
        self.dp.callback_query.register(self._cb_model, F.data.startswith("model:"))
        self.dp.callback_query.register(self._cb_provider, F.data.startswith("prov:"))

        # Регистрируем меню команд в Telegram
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="restart", description="Перезагрузить Jinx"),
                    BotCommand(command="status", description="Статус агента"),
                    BotCommand(command="models", description="Список моделей"),
                    BotCommand(command="stop", description="Остановить Jinx"),
                    BotCommand(command="help", description="Помощь"),
                ],
                scope=BotCommandScopeAllPrivateChats(),
            )
        except Exception as e:
            system_logger.warning(f"[Telegram] Не удалось зарегистрировать меню: {e}")

        # Регистрация хендлеров сообщений
        self.dp.message.register(self._on_private_message, F.chat.type == "private")
        self.dp.message.register(
            self._on_group_message, F.chat.type.in_({"group", "supergroup"})
        )

        self.dp.message.register(
            self._on_system_message,
            F.content_type.in_(
                {
                    "new_chat_members",
                    "left_chat_member",
                    "new_chat_title",
                    "new_chat_photo",
                    "delete_chat_photo",
                    "pinned_message",
                }
            ),
        )

        # Сбрасываем старые апдейты, чтобы бот не отвечал на то, что накопилось пока он был выключен
        await bot.delete_webhook(drop_pending_updates=True)

        # Запускаем поллинг как фоновую задачу
        self._polling_task = asyncio.create_task(self.dp.start_polling(bot))
        system_logger.info("[Telegram Aiogram] Фоновый поллинг запущен.")

    async def stop(self) -> None:
        """Останавливает поллинг."""

        if self._polling_task:
            self._polling_task.cancel()
            self._polling_task = None

        # Корректно закрываем Dispatcher
        try:
            await self.dp.stop_polling()
        except RuntimeError:
            pass  # Игнорируем ошибку "Polling is not started", если он не успел запуститься

        system_logger.info("[Telegram Aiogram] Фоновый поллинг остановлен.")

    async def _update_state(self, message: Message):
        """
        Сохраняет чат в кэш и формирует строку для приборной панели.
        Работает по принципу MRU (Most Recently Used).
        """

        chat_type = "User" if message.chat.type == "private" else "Group"
        chat_name = message.chat.title or message.chat.full_name or message.from_user.full_name

        # Сохраняем/обновляем чат в словаре (ключи в dict сохраняют порядок добавления с Python 3.7+)
        chat_str = f"{chat_type} | ID: {message.chat.id} | Название: {chat_name}"

        # Удаляем, чтобы при добавлении он оказался в конце (самым свежим)
        self.state._chats_cache.pop(message.chat.id, None)
        self.state._chats_cache[message.chat.id] = chat_str

        # Оставляем только последние N
        if len(self.state._chats_cache) > self.state.number_of_last_chats:
            first_key = next(iter(self.state._chats_cache))
            del self.state._chats_cache[first_key]

        # Переворачиваем, чтобы новые были сверху
        lines = list(self.state._chats_cache.values())[::-1]
        self.state.last_chats = "\n".join(lines)

    async def _on_private_message(self, message: Message):
        """Триггер на сообщения в ЛС бота."""

        await self._update_state(message)
        self.agent_state.incoming_chat_id = message.chat.id

        sender_name = message.from_user.first_name if message.from_user else "Unknown"

        await self.bus.publish(
            Events.AIOGRAM_MESSAGE_INCOMING,
            message=message.text or message.caption or "[Медиа]",
            sender_name=sender_name,
            chat_id=message.chat.id,
            msg_id=message.message_id,
        )

    async def _on_group_message(self, message: Message):
        """Триггер на сообщения в группах."""

        await self._update_state(message)
        self.agent_state.incoming_chat_id = message.chat.id

        bot = self.client.bot()
        me = await bot.get_me()

        # Проверяем, тегнули ли бота: через @username или через reply
        is_mentioned = False
        if message.text and me.username in message.text:
            is_mentioned = True
        elif message.reply_to_message and message.reply_to_message.from_user.id == me.id:
            is_mentioned = True

        event_type = (
            Events.AIOGRAM_GROUP_MENTION if is_mentioned else Events.AIOGRAM_GROUP_MESSAGE
        )
        sender_name = message.from_user.first_name if message.from_user else "Unknown"

        await self.bus.publish(
            event_type,
            message=message.text or message.caption or "[Медиа]",
            sender_name=sender_name,
            chat_id=message.chat.id,
            msg_id=message.message_id,
        )

    async def _on_system_message(self, message: Message):
        """Триггер на системные события (вход, выход, смена названия и т.д.)."""

        await self._update_state(message)

        action_text = "[Системное действие]"

        if message.new_chat_members:
            users = ", ".join([u.first_name for u in message.new_chat_members if u.first_name])
            action_text = f"[Системное действие] {users} присоединился к чату."

        elif message.left_chat_member:
            action_text = (
                f"[Системное действие] {message.left_chat_member.first_name} покинул чат."
            )

        elif message.new_chat_title:
            action_text = (
                f"[Системное действие] Название чата изменено на '{message.new_chat_title}'."
            )

        elif message.pinned_message:
            action_text = "[Системное действие] Закреплено новое сообщение."

        elif message.new_chat_photo or message.delete_chat_photo:
            action_text = "[Системное действие] Фото чата было изменено/удалено."

        payload = {
            "message": action_text,
            "sender_name": "System",
            "chat_id": message.chat.id,
        }

        await self.bus.publish(Events.AIOGRAM_CHAT_ACTION, **payload)

    # ===========================================
    # COMMAND HANDLERS
    # ===========================================

    def _load_models_config(self) -> dict | None:
        try:
            with open(self._config_path) as f:
                return json.load(f)
        except Exception:
            return None

    async def _cmd_help(self, message: Message):
        """Показать список команд."""
        lines = [
            "⚔️ *Jinx Commands:*",
            "",
            "/status — текущее состояние",
            "/models — сменить модель",
            "/restart — перезагрузка",
            "/stop — остановка",
            "/help — эта справка",
        ]
        await message.reply("\n".join(lines), parse_mode="Markdown")

    async def _cmd_status(self, message: Message):
        """Показать статус агента + проверка Telegram."""
        s = self.agent_state
        status_emoji = {
            AgentStatus.IDLE: "💤",
            AgentStatus.THINKING: "🧠",
            AgentStatus.ACTING: "⚡",
            AgentStatus.ERROR: "🔴",
        }.get(s.state, "❓")

        # Проверка Telegram polling
        tg_status = "❓ неизвестно"
        tg_queue = 0
        try:
            bot = self.client.bot()
            # Проверяем polling через get_webhook_info (если webhook установлен — polling не работает)
            wh_info = await bot.get_webhook_info()
            if wh_info.url:
                tg_status = "⚠️ webhook установлен (polling не работает)"
            elif self._polling_task and not self._polling_task.done():
                tg_status = "🟢 polling жив"
            else:
                tg_status = "🔴 polling мёртв"

            # Проверяем unconsumed messages (getUpdates с offset=0 не подтверждает)
            try:
                updates = await bot.get_updates(offset=-1, limit=1, timeout=0)
                last_update = updates[0] if updates else None
                if last_update:
                    # Считаем сколько времени сообщение висит
                    # getUpdates с offset=-1 возвращает последнее неподтверждённое
                    all_pending = await bot.get_updates(offset=0, limit=100, timeout=0)
                    tg_queue = len(all_pending)
                    if tg_queue > 0:
                        tg_status += f"\n📬 В очереди: {tg_queue} сообщений"
                else:
                    tg_queue = 0
                    tg_status += "\n📬 В очереди: 0"
            except Exception as e:
                system_logger.warning(f"[Telegram] getUpdates failed: {e}")
                tg_status += f"\n⚠️ getUpdates: {e}"

        except Exception as e:
            tg_status = f"🔴 ошибка: {e}"

        lines = [
            f"{status_emoji} *Jinx Status*",
            "",
            f"Model: `{s.llm_model}`",
            f"Step: {s.current_step}/{s.max_react_steps}",
            f"Uptime: {s.get_uptime()}",
            f"Temperature: {s.temperature}",
            f"Heartbeat: {s.heartbeat_interval}s",
            f"Missed tools: {s.missed_tool_calls}",
            "",
            f"Telegram: {tg_status}",
        ]
        await message.reply("\n".join(lines), parse_mode="Markdown")

    async def _cmd_models(self, message: Message):
        """Показать inline-кнопки для выбора провайдера."""
        config = self._load_models_config()
        if not config:
            await message.reply("❌ Не удалось прочитать config/models.json")
            return

        current_provider = config.get("default_provider", "")
        current_model = config.get("default_model", "")

        buttons = []
        for pname, pdata in config.get("providers", {}).items():
            models = pdata.get("models", [])
            if not models:
                continue
            model_count = len(models)
            active = " ◀️" if pname == current_provider else ""
            buttons.append([
                InlineKeyboardButton(
                    text=f"{pname}{active} ({model_count})",
                    callback_data=f"prov:{pname}",
                )
            ])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.reply(
            f"📋 *Выберите провайдер:*\nТекущая: `{current_provider}` / `{current_model}`",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    async def _cb_provider(self, callback: CallbackQuery):
        """Показать модели выбранного провайдера."""
        provider_id = callback.data.split(":", 1)[1]
        config = self._load_models_config()
        if not config:
            await callback.answer("❌ Ошибка конфигурации")
            return

        pdata = config.get("providers", {}).get(provider_id)
        if not pdata:
            await callback.answer("❌ Провайдер не найден")
            return

        current_model = config.get("default_model", "")
        current_provider = config.get("default_provider", "")

        buttons = []
        for m in pdata.get("models", []):
            mid = m.get("id", "?")
            mname = m.get("name", mid)
            active = " ✅" if provider_id == current_provider and mid == current_model else ""
            buttons.append([
                InlineKeyboardButton(
                    text=f"{mname}{active}",
                    callback_data=f"model:{provider_id}:{mid}",
                )
            ])

        # Back button
        buttons.append([
            InlineKeyboardButton(text="← Назад", callback_data="prov:back")
        ])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        try:
            await callback.message.edit_text(
                f"📋 *{provider_id} — модели:*",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception:
            await callback.answer("Ок")

    async def _cb_model(self, callback: CallbackQuery):
        """Переключить модель."""
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer("❌ Неверный формат")
            return

        provider_id = parts[1]
        model_id = parts[2]

        config = self._load_models_config()
        if not config:
            await callback.answer("❌ Ошибка конфигурации")
            return

        # Validate
        pdata = config.get("providers", {}).get(provider_id)
        if not pdata:
            await callback.answer("❌ Провайдер не найден")
            return

        model_found = any(m["id"] == model_id for m in pdata.get("models", []))
        if not model_found:
            await callback.answer("❌ Модель не найдена")
            return

        # Update config
        config["default_provider"] = provider_id
        config["default_model"] = model_id

        try:
            with open(self._config_path, "w") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            await callback.answer(f"❌ Ошибка записи: {e}")
            return

        model_name = next(
            (m.get("name", model_id) for m in pdata["models"] if m["id"] == model_id),
            model_id,
        )

        await callback.message.edit_text(
            f"✅ Модель: *{model_name}*\n"
            f"Провайдер: `{provider_id}`\n\n"
            f"🔄 Перезагрузка...",
            parse_mode="Markdown",
        )
        system_logger.info(f"[Telegram] Модель переключена: {provider_id}/{model_id} (от пользователя)")
        await self.bus.publish(Events.SYSTEM_REBOOT_REQUESTED)

    async def _cb_back(self, callback: CallbackQuery):
        """Вернуться к списку провайдеров."""
        config = self._load_models_config()
        if not config:
            await callback.answer("❌ Ошибка")
            return

        current_provider = config.get("default_provider", "")
        current_model = config.get("default_model", "")

        buttons = []
        for pname, pdata in config.get("providers", {}).items():
            models = pdata.get("models", [])
            if not models:
                continue
            active = " ◀️" if pname == current_provider else ""
            buttons.append([
                InlineKeyboardButton(
                    text=f"{pname}{active} ({len(models)})",
                    callback_data=f"prov:{pname}",
                )
            ])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        try:
            await callback.message.edit_text(
                f"📋 *Выберите провайдер:*\nТекущая: `{current_provider}` / `{current_model}`",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception:
            await callback.answer("Ок")

    async def _cmd_restart(self, message: Message):
        """Перезагрузить Jinx."""
        await message.reply("🔄 Перезагрузка...")
        system_logger.info("[Telegram] /restart — запрос перезагрузки от пользователя")
        await self.bus.publish(Events.SYSTEM_REBOOT_REQUESTED)

    async def _cmd_stop(self, message: Message):
        """Остановить Jinx."""
        await message.reply("⏹ Остановка...")
        system_logger.info("[Telegram] /stop — запрос остановки от пользователя")
        await self.bus.publish(Events.SYSTEM_SHUTDOWN_REQUESTED)
