"""Access-control helpers (owner / admins / bans / group rules)."""
import config
from database.db import Database


def is_owner(user_id: int) -> bool:
    return user_id == config.OWNER_ID


async def is_admin(db: Database, user_id: int) -> bool:
    return is_owner(user_id) or await db.is_admin(user_id)


async def can_use_bot(db: Database, user_id: int) -> bool:
    """Banned users cannot use the bot at all."""
    return not await db.is_user_banned(user_id)


async def group_allowed(db: Database, chat_id: int) -> bool:
    """Blacklisted groups are auto-left; streaming may be disabled per group."""
    return not await db.is_group_blacklisted(chat_id)
