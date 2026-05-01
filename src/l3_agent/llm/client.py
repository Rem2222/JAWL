import os
import json
from openai import AsyncOpenAI

from src.utils.logger import system_logger


def load_models_config(config_path: str = "/home/rem/JAWL/config/models.json") -> dict:
    """Load models.json configuration."""
    with open(config_path) as f:
        return json.load(f)


class LLMClient:
    """
    Интерфейс для общения мозга агента с языковой моделью.
    Читает конфигурацию из models.json.
    """

    def __init__(self, config_path: str = "/home/rem/JAWL/config/models.json"):
        self.config_path = config_path
        self._config: dict = {}
        self._sessions: dict[str, AsyncOpenAI] = {}

        self.load_config()

    def load_config(self) -> None:
        """Load/reload configuration from models.json."""
        self._config = load_models_config(self.config_path)
        default_provider = self._config.get("default_provider", "")
        default_model = self._config.get("default_model", "")
        system_logger.info(f"[LLM] Конфиг загружен: provider={default_provider}, model={default_model}")

    @property
    def default_provider(self) -> str:
        return self._config.get("default_provider", "")

    @property
    def default_model(self) -> str:
        return self._config.get("default_model", "")

    def get_provider_config(self, provider_id: str) -> dict | None:
        """Return config for a specific provider."""
        return self._config.get("providers", {}).get(provider_id)

    def get_model_config(self, provider_id: str, model_id: str) -> dict | None:
        """Return config for a specific model within a provider."""
        provider = self.get_provider_config(provider_id)
        if not provider:
            return None
        for m in provider.get("models", []):
            if m["id"] == model_id:
                return m
        return None

    def resolve_api_key(self, provider_id: str) -> str | None:
        """Resolve API key from env variable name or direct value."""
        provider = self.get_provider_config(provider_id)
        if not provider:
            return None
        api_key = provider.get("apiKey")
        if api_key is None:
            return None
        if api_key.startswith("LLM_API_KEY"):
            return os.environ.get(api_key, "")
        # Direct key value (not used in JAWL but supported for compatibility)
        return api_key

    def get_session(self, provider_id: str | None = None, model_id: str | None = None) -> AsyncOpenAI:
        """
        Returns a cached OpenAI session for the provider.
        Resolves API key from env variable name defined in models.json.
        """
        provider_id = provider_id or self.default_provider
        provider = self.get_provider_config(provider_id)

        if not provider:
            raise RuntimeError(f"[LLM] Unknown provider: {provider_id}")

        base_url = provider.get("baseUrl", "")
        api_key = self.resolve_api_key(provider_id)

        # Normalize URL
        if base_url and not base_url.startswith(("http://", "https://")):
            if "localhost" in base_url or "127.0.0.1" in base_url:
                base_url = f"http://{base_url}"
            else:
                base_url = f"https://{base_url}"

        # Session key: use provider_id for simplicity
        session_key = provider_id

        if session_key in self._sessions:
            return self._sessions[session_key]

        # Create session
        if api_key:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        else:
            # No API key (e.g. opencode-zen)
            client = AsyncOpenAI(api_key="", base_url=base_url)

        self._sessions[session_key] = client
        system_logger.info(f"[LLM] Сессия создана: provider={provider_id}, baseUrl={base_url}, has_key={bool(api_key)}")
        return client

    def get_api_format(self, provider_id: str | None = None) -> str:
        """Return API format for the provider (openai-completions | openai-responses | anthropic-messages)."""
        provider_id = provider_id or self.default_provider
        provider = self.get_provider_config(provider_id)
        if not provider:
            return "openai-completions"
        return provider.get("api", "openai-completions")

    async def close(self) -> None:
        """Cleanly close all active HTTP connection pools."""
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()
        system_logger.info("[LLM] Все HTTP-сессии закрыты.")
