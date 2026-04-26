"""Simplified send wrapper for JAWL - ultra-simple send_message for the LLM."""
from src.l2_interfaces.telegram.aiogram.client import AiogramClient
from src.l3_agent.skills.registry import SkillResult, skill
from src.utils.logger import system_logger


class SimpleSend:
    """
    Ультра-простая обёртка для отправки сообщений.
    chat_id захардкожен, модель передаёт только текст.
    """

    def __init__(self, aiogram_client: AiogramClient):
        self.client = aiogram_client
        # Захардкоженный chat_id для Романа
        self.default_chat_id = 386235337

    @skill()
    async def send_message(self, text: str) -> SkillResult:
        """
        Отправляет текстовое сообщение Роману.
        Параметры: text (строка) - текст сообщения.
        """
        if not text or not text.strip():
            return SkillResult.fail("Пустое сообщение.")

        try:
            bot = self.client.bot()
            msg = await bot.send_message(
                chat_id=self.default_chat_id,
                text=text.strip()
            )
            system_logger.info(f"[SimpleSend] Отправлено сообщение {msg.message_id} в чат {self.default_chat_id}")
            return SkillResult.ok(f"Отправлено! ID: {msg.message_id}")

        except Exception as e:
            system_logger.error(f"[SimpleSend] Ошибка: {e}")
            return SkillResult.fail(f"Ошибка отправки: {e}")
