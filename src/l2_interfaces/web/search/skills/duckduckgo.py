import asyncio
import time
from duckduckgo_search import DDGS
from duckduckgo_search.exceptions import RatelimitException
from typing import Any

from src.utils.logger import system_logger
from src.l3_agent.skills.registry import skill, SkillResult
from src.l2_interfaces.web.search.client import WebSearchClient


class DuckDuckGoSearch:
    """Навыки поиска информации в интернете (DuckDuckGo)."""

    def __init__(self, client: WebSearchClient):
        self.client = client

    async def search_raw(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        """Сырой поиск с retry при rate limit."""

        def _do_search():
            # Retry up to 3 times with backoff: 3s, 6s, 12s
            for attempt in range(4):
                try:
                    with DDGS() as ddgs:
                        return list(ddgs.text(query, max_results=max_results))
                except RatelimitException:
                    if attempt < 3:
                        delay = 3 * (2 ** attempt)
                        time.sleep(delay)
                    else:
                        raise
                except Exception as e:
                    # Other errors: fail immediately
                    raise

        return await asyncio.to_thread(_do_search)

    @skill()
    async def web_search(self, query: str, max_results: int = 5) -> SkillResult:
        """Ищет информацию в интернете. Возвращает список ссылок и кратких сниппетов."""

        try:
            results = await self.search_raw(query, max_results)

            if not results:
                return SkillResult.ok(f"По запросу '{query}' ничего не найдено.")

            formatted = [
                f"Title: {r.get('title')}\nURL: {r.get('href')}\nSnippet: {r.get('body')}"
                for r in results
            ]

            system_logger.info(f"[Web] Выполнен поиск по запросу: '{query}'")
            self.client.state.add_history(f"Поиск DDG: '{query}'")
            return SkillResult.ok("\n\n".join(formatted))

        except Exception as e:
            return SkillResult.fail(f"Ошибка веб-поиска: {e}")
