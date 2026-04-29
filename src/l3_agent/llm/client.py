from openai import AsyncOpenAI

from src.utils.logger import system_logger


class LLMClient:
    """
    Интерфейс для общения мозга агента с языковой моделью.
    """

    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url
        self.api_key = api_key

        # Кэш сессий для переиспользования соединений и предотвращения утечек сокетов
        self._sessions: dict[str, AsyncOpenAI] = {}

        # Нормализация URL
        if self.api_url and not self.api_url.startswith(("http://", "https://")):
            if "localhost" in self.api_url or "127.0.0.1" in self.api_url:
                self.api_url = f"http://{self.api_url}"
            else:
                self.api_url = f"https://{self.api_url}"

        if self.api_url:
            system_logger.info(f"[LLM] Клиент инициализирован (URL: {self.api_url}).")
        else:
            system_logger.info("[LLM] Клиент инициализирован (Default OpenAI URL).")

    def get_session(self) -> AsyncOpenAI:
        """
        Возвращает закэшированную сессию OpenAI с актуальным ключом.
        Для OpenCode Responses API (не требует API ключа).
        """
        # OpenCode Zen/Go APIs работают БЕЗ API ключа
        if "opencode.ai/zen/" in self.api_url:
            no_key = "_NO_KEY_"
            if no_key not in self._sessions:
                self._sessions[no_key] = AsyncOpenAI(api_key="", base_url=self.api_url)
            return self._sessions[no_key]

        if not self.api_key:
            raise RuntimeError("[LLM] Нет API ключа.")

        # Ленивая инициализация: создаем клиента только при первом обращении
        if self.api_key not in self._sessions:
            self._sessions[self.api_key] = AsyncOpenAI(api_key=self.api_key, base_url=self.api_url)
        return self._sessions[self.api_key]

    async def close(self) -> None:
        """Корректно закрывает все активные пулы HTTP-соединений."""
        for session in self._sessions.values():
            await session.close()

        self._sessions.clear()
        system_logger.info("[LLM] Все HTTP-сессии закрыты.")
