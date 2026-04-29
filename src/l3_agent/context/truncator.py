"""
Контекстный трюнкатор — урезает контекст до безопасного размера.

Используется в ReactLoop для динамического ограничения контекста,
когда тот превышает бюджет токенов.
"""

from typing import List, Dict, Any

from src.utils.logger import system_logger


class ContextTruncator:
    """
    Урезает контекстные данные (missed_events, ticks) чтобы
    суммарный контекст влез в бюджет токенов.
    """

    # Сколько символов примерно в одном токене (зависит от языка)
    CHARS_PER_TOKEN = 3.5

    def __init__(
        self,
        max_context_tokens: int = 25000,
    ):
        self.max_context_tokens = max_context_tokens

    def estimate_tokens(self, text: str) -> int:
        """Быстрая оценка токенов в тексте."""
        return int(len(text) / self.CHARS_PER_TOKEN)

    def should_truncate(self, context: str) -> bool:
        """Проверяет, нужно ли урезать контекст."""
        tokens = self.estimate_tokens(context)
        return tokens > self.max_context_tokens

    def truncate_missed_events(
        self,
        missed_events: List[Dict[str, Any]],
        current_tokens: int,
        target_tokens: int,
    ) -> List[Dict[str, Any]]:
        """
        Урезает список missed_events чтобы вписаться в бюджет.
        Возвращает урезанный список.
        """
        if not missed_events:
            return missed_events

        if current_tokens <= target_tokens:
            return missed_events

        # Считаем сколько токенов нужно убрать
        tokens_to_remove = current_tokens - target_tokens

        # Считаем средний размер события
        total_chars = sum(len(str(e)) for e in missed_events)
        avg_chars_per_event = total_chars / len(missed_events)
        avg_tokens_per_event = avg_chars_per_event / self.CHARS_PER_TOKEN

        # Сколько событий убрать
        events_to_remove = int(tokens_to_remove / avg_tokens_per_event) + 1
        events_to_remove = min(events_to_remove, len(missed_events) - 1)  # оставить хотя бы 1

        # Убираем самые старые события
        kept_events = missed_events[events_to_remove:]

        system_logger.info(
            f"[ContextTruncator] Truncated {events_to_remove} missed_events "
            f"(was {len(missed_events)}, now {len(kept_events)})"
        )

        return kept_events

    def truncate_recent_history(
        self,
        recent_history: str,
        current_tokens: int,
        target_tokens: int,
    ) -> str:
        """
        Урезает recent_history до нужного размера.
        """
        if not recent_history:
            return recent_history

        if current_tokens <= target_tokens:
            return recent_history

        tokens_to_remove = current_tokens - target_tokens
        chars_to_remove = int(tokens_to_remove * self.CHARS_PER_TOKEN)

        # recent_history - это обычно форматированный текст
        # Пробуем вырезать середину, оставив начало и конец
        lines = recent_history.split("\n")

        if len(lines) > 6:
            # Оставляем первые 3 и последние 3 строки
            kept_lines = lines[:3] + ["... (truncated) ..."] + lines[-3:]
            result = "\n".join(kept_lines)

            if self.estimate_tokens(result) <= target_tokens:
                system_logger.info(
                    f"[ContextTruncator] Truncated recent_history: {len(recent_history)} → {len(result)} chars"
                )
                return result

        # Фоллбэк: просто обрезаем конец
        result = recent_history[:len(recent_history) - chars_to_remove]
        system_logger.info(
            f"[ContextTruncator] Truncated recent_history (simple): {len(recent_history)} → {len(result)} chars"
        )
        return result
