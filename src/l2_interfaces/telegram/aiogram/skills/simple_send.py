"""Simplified send wrapper for JAWL - ultra-simple send_message for the LLM."""
import hashlib
import time
from src.l2_interfaces.telegram.aiogram.client import AiogramClient
from src.l3_agent.skills.registry import SkillResult, skill
from src.utils.logger import system_logger

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.l0_state.agent.state import AgentState


class SimpleSend:
    """
    Ультра-простая обёртка для отправки сообщений.
    Отвечает туда, откуда пришло последнее сообщение (incoming_chat_id).
    Fallback — ЛС Романа (386235337) для heartbeat и spontaneous.
    """

    # Хэши отправленных сообщений: {hash: timestamp}
    _sent_hashes: dict[str, float] = {}
    # Префиксы для быстрой проверки дубликатов
    _sent_prefixes: dict[str, float] = {}
    # TTL в секундах
    THROTTLE_TTL_SEC: float = 600  # 10 минут

    FALLBACK_CHAT_ID: int = 386235337  # ЛС Романа

    def __init__(self, aiogram_client: AiogramClient, agent_state: "AgentState"):
        self.client = aiogram_client
        self.agent_state = agent_state

    def _content_hash(self, text: str) -> str:
        """Хэш без учёта регистра и пробелов."""
        normalized = text.strip().lower()
        return hashlib.sha256(normalized.encode()).hexdigest()

    def _is_duplicate(self, text: str) -> bool:
        """Проверка дубликата по TTL. Сравнивает префикс (first 60 chars)."""
        h = self._content_hash(text)
        # Префикс = первые 60 символов (без учёта регистра)
        prefix = text[:60].strip().lower()
        prefix_hash = hashlib.sha256(prefix.encode()).hexdigest()
        now = time.time()
        # Очистка просроченных записей
        for d in [self._sent_hashes, self._sent_prefixes]:
            expired = [k for k, t in list(d.items()) if now - t > self.THROTTLE_TTL_SEC]
            for k in expired:
                del d[k]
        # Проверка: если такой префикс уже был - это дубликат
        if prefix_hash in self._sent_prefixes:
            return True
        # Сохраняем оба хэша
        self._sent_hashes[h] = now
        self._sent_prefixes[prefix_hash] = now
        return False

    @skill()
    async def send_message(self, text: str) -> SkillResult:
        """
        Отправляет текстовое сообщение.
        Отвечает в чат, откуда пришло последнее сообщение.
        Параметры: text (строка) - текст сообщения.
        """
        if not text or not text.strip():
            return SkillResult.fail("Пустое сообщение.")

        # Throttle check - дубликат по префиксу
        if self._is_duplicate(text):
            system_logger.info(f"[SimpleSend] Дубликат сообщения, пропускаем. Префикс: {hashlib.sha256(text[:60].strip().lower().encode()).hexdigest()[:16]}...")
            return SkillResult.ok(f"Пропущено (дубликат, <{int(self.THROTTLE_TTL_SEC//60)} мин)")

        # Хардблок: SYSTEM CORE START в любом формате (пробелы или подчёркивания)
        normalized_text = text.strip().lower()
        if "system core start" in normalized_text or "system_core_start" in normalized_text:
            system_logger.info("[SimpleSend] Блокировка: 'SYSTEM CORE START' в тексте")
            return SkillResult.ok("Блокировано: SYSTEM CORE START уведомления запрещены")

        chat_id = self.agent_state.incoming_chat_id

        try:
            bot = self.client.bot()
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text.strip()
            )
            system_logger.info(f"[SimpleSend] Отправлено сообщение {msg.message_id} в чат {chat_id}")
            return SkillResult.ok(f"Отправлено! ID: {msg.message_id}")

        except Exception as e:
            system_logger.error(f"[SimpleSend] Ошибка: {e}")
            return SkillResult.fail(f"Ошибка отправки: {e}")
