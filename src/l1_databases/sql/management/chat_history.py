from typing import Optional, TYPE_CHECKING
from sqlalchemy import select, desc, and_, delete
from datetime import datetime, timezone

from src.utils.logger import system_logger
from src.l1_databases.sql.tables import ChatHistoryTable

if TYPE_CHECKING:
    from src.l1_databases.sql.db import SQLDB


class SQLChatHistory:
    """
    CRUD-функции для постоянного хранения истории сообщений чата.
    Обеспечивает persistence chat history между перезапусками JAWL.
    """

    def __init__(
        self,
        db: "SQLDB",
        max_messages_per_chat: int = 100,
    ):
        self.db = db
        self.max_messages_per_chat = max_messages_per_chat

    async def save_message(
        self,
        chat_id: int,
        message_id: int,
        sender_name: str,
        text: str,
        direction: str,  # "incoming" or "outgoing"
    ) -> int:
        """Сохраняет одно сообщение в историю чата."""

        async with self.db.session_factory() as session:
            new_msg = ChatHistoryTable(
                chat_id=chat_id,
                message_id=message_id,
                sender_name=sender_name,
                text=text,
                direction=direction,
            )
            session.add(new_msg)
            await session.commit()
            msg_id = new_msg.id

        system_logger.debug(
            f"[SQL ChatHistory] Saved message {message_id} in chat {chat_id} ({direction})"
        )
        return msg_id

    async def get_recent_history(
        self,
        chat_id: int,
        limit: int = 20,
    ) -> list[ChatHistoryTable]:
        """Возвращает последние N сообщений из указанного чата."""

        async with self.db.session_factory() as session:
            stmt = (
                select(ChatHistoryTable)
                .where(ChatHistoryTable.chat_id == chat_id)
                .order_by(desc(ChatHistoryTable.created_at))
                .limit(limit)
            )
            result = await session.execute(stmt)
            messages = result.scalars().all()
            # Возвращаем в хронологическом порядке (старые first)
            return list(reversed(messages))

    async def get_context_block(
        self,
        chat_id: int,
        limit: int = 20,
    ) -> str:
        """
        Возвращает отформатированный блок контекста с историей чата.
        """
        messages = await self.get_recent_history(chat_id=chat_id, limit=limit)

        if not messages:
            return ""

        blocks = []
        for msg in messages:
            direction_tag = "←" if msg.direction == "incoming" else "→"
            time_str = msg.created_at.strftime("%H:%M")
            blocks.append(f"{direction_tag} [{time_str}] {msg.sender_name}: {msg.text}")

        history_text = "\n".join(blocks)
        return f"\n\n## CHAT HISTORY (persistent, {len(messages)} messages)\n{history_text}\n"

    async def cleanup_old_messages(self, chat_id: int) -> int:
        """
        Удаляет старые сообщения сверх лимита max_messages_per_chat.
        Возвращает количество удаленных сообщений.
        """
        async with self.db.session_factory() as session:
            # Находим все сообщения для этого чата, отсортированные по дате (новые first)
            stmt = (
                select(ChatHistoryTable.id)
                .where(ChatHistoryTable.chat_id == chat_id)
                .order_by(desc(ChatHistoryTable.created_at))
            )
            result = await session.execute(stmt)
            all_ids = [row[0] for row in result.all()]

            if len(all_ids) <= self.max_messages_per_chat:
                return 0

            # Удаляем все, что выходит за лимит
            ids_to_delete = all_ids[self.max_messages_per_chat:]
            if ids_to_delete:
                from sqlalchemy import delete as sql_delete
                delete_stmt = sql_delete(ChatHistoryTable).where(
                    ChatHistoryTable.id.in_(ids_to_delete)
                )
                await session.execute(delete_stmt)
                await session.commit()
                system_logger.debug(
                    f"[SQL ChatHistory] Cleaned up {len(ids_to_delete)} old messages from chat {chat_id}"
                )
            return len(ids_to_delete)
