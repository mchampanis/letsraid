import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS lfg_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    voice_channel_id INTEGER,
    mode TEXT NOT NULL CHECK(mode IN ('pvp', 'pve')),
    description TEXT,
    start_time TEXT,
    max_slots INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'full', 'closed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    guild_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lfg_members (
    lfg_id INTEGER NOT NULL REFERENCES lfg_posts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    joined_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (lfg_id, user_id)
);

CREATE TABLE IF NOT EXISTS lfg_board (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
"""


async def init_db(db: aiosqlite.Connection):
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(SCHEMA)
    await db.execute("DROP TABLE IF EXISTS lfg_roles")

    # Migration: add guild_seq column if missing
    async with db.execute("PRAGMA table_info(lfg_posts)") as cursor:
        columns = [row[1] async for row in cursor]
    if "guild_seq" not in columns:
        await db.execute("ALTER TABLE lfg_posts ADD COLUMN guild_seq INTEGER NOT NULL DEFAULT 0")
        # Backfill existing rows
        await db.execute("""
            UPDATE lfg_posts SET guild_seq = (
                SELECT COUNT(*) FROM lfg_posts p2
                WHERE p2.guild_id = lfg_posts.guild_id AND p2.id <= lfg_posts.id
            )
        """)

    await db.commit()


async def create_lfg(
    db: aiosqlite.Connection,
    *,
    message_id: int,
    channel_id: int,
    guild_id: int,
    creator_id: int,
    voice_channel_id: int | None,
    mode: str,
    description: str | None,
    start_time: str | None,
    max_slots: int,
) -> tuple[int, int]:
    # Compute next per-guild sequence number
    async with db.execute(
        "SELECT COALESCE(MAX(guild_seq), 0) + 1 FROM lfg_posts WHERE guild_id = ?",
        (guild_id,),
    ) as cursor:
        next_seq = (await cursor.fetchone())[0]

    cursor = await db.execute(
        """INSERT INTO lfg_posts
           (message_id, channel_id, guild_id, creator_id, voice_channel_id,
            mode, description, start_time, max_slots, guild_seq)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (message_id, channel_id, guild_id, creator_id, voice_channel_id,
         mode, description, start_time, max_slots, next_seq),
    )
    lfg_id = cursor.lastrowid

    # Add creator as first member
    await db.execute(
        "INSERT INTO lfg_members (lfg_id, user_id) VALUES (?, ?)",
        (lfg_id, creator_id),
    )

    await db.commit()
    return lfg_id, next_seq


async def update_message_id(db: aiosqlite.Connection, lfg_id: int, message_id: int):
    await db.execute(
        "UPDATE lfg_posts SET message_id = ? WHERE id = ?", (message_id, lfg_id)
    )
    await db.commit()


async def get_lfg(db: aiosqlite.Connection, lfg_id: int) -> dict | None:
    async with db.execute(
        "SELECT * FROM lfg_posts WHERE id = ?", (lfg_id,)
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_post_by_vc(
    db: aiosqlite.Connection,
    guild_id: int,
    voice_channel_id: int,
    exclude_lfg_id: int | None = None,
) -> dict | None:
    """Return the open/full post linked to this voice channel (if any).

    Voice channel uniqueness across active posts is enforced at write time, so
    at most one row will match. `exclude_lfg_id` lets the caller skip a known
    post (used by ChangeVC to check if the *target* VC is taken by someone else).
    """
    query = """SELECT * FROM lfg_posts
               WHERE guild_id = ? AND voice_channel_id = ? AND status IN ('open', 'full')"""
    params: tuple = (guild_id, voice_channel_id)
    if exclude_lfg_id is not None:
        query += " AND id != ?"
        params = (*params, exclude_lfg_id)
    query += " LIMIT 1"
    async with db.execute(query, params) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_post_for_user(db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict | None:
    """Return the open/full post this user is a member of (if any)."""
    async with db.execute(
        """SELECT p.* FROM lfg_posts p
           JOIN lfg_members m ON m.lfg_id = p.id
           WHERE p.guild_id = ? AND m.user_id = ? AND p.status IN ('open', 'full')
           LIMIT 1""",
        (guild_id, user_id),
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_lfg_members(db: aiosqlite.Connection, lfg_id: int) -> list[int]:
    async with db.execute(
        "SELECT user_id FROM lfg_members WHERE lfg_id = ? ORDER BY joined_at",
        (lfg_id,),
    ) as cursor:
        return [row["user_id"] async for row in cursor]


async def add_member(db: aiosqlite.Connection, lfg_id: int, user_id: int) -> bool:
    """Add a member if the party isn't full. Returns False on duplicate or full party."""
    try:
        cursor = await db.execute(
            """INSERT INTO lfg_members (lfg_id, user_id)
               SELECT ?, ?
               WHERE (SELECT COUNT(*) FROM lfg_members WHERE lfg_id = ?)
                     < (SELECT max_slots FROM lfg_posts WHERE id = ?)""",
            (lfg_id, user_id, lfg_id, lfg_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    except aiosqlite.IntegrityError:
        return False


async def remove_member(db: aiosqlite.Connection, lfg_id: int, user_id: int) -> bool:
    cursor = await db.execute(
        "DELETE FROM lfg_members WHERE lfg_id = ? AND user_id = ?",
        (lfg_id, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_voice_channel(db: aiosqlite.Connection, lfg_id: int, voice_channel_id: int | None):
    await db.execute(
        "UPDATE lfg_posts SET voice_channel_id = ? WHERE id = ?", (voice_channel_id, lfg_id)
    )
    await db.commit()


async def update_status(db: aiosqlite.Connection, lfg_id: int, status: str):
    if status not in ("open", "full", "closed"):
        raise ValueError(f"Invalid status: {status!r}")
    await db.execute(
        "UPDATE lfg_posts SET status = ? WHERE id = ?", (status, lfg_id)
    )
    await db.commit()


async def delete_lfg(db: aiosqlite.Connection, lfg_id: int):
    await db.execute("DELETE FROM lfg_posts WHERE id = ?", (lfg_id,))
    await db.commit()


async def get_open_posts(db: aiosqlite.Connection, guild_id: int) -> list[dict]:
    async with db.execute(
        "SELECT * FROM lfg_posts WHERE guild_id = ? AND status IN ('open', 'full') ORDER BY status = 'full', created_at DESC",
        (guild_id,),
    ) as cursor:
        return [dict(row) async for row in cursor]


async def set_board(db: aiosqlite.Connection, guild_id: int, channel_id: int, message_id: int):
    await db.execute(
        "INSERT OR REPLACE INTO lfg_board (guild_id, channel_id, message_id) VALUES (?, ?, ?)",
        (guild_id, channel_id, message_id),
    )
    await db.commit()


async def get_board(db: aiosqlite.Connection, guild_id: int) -> dict | None:
    async with db.execute(
        "SELECT * FROM lfg_board WHERE guild_id = ?", (guild_id,)
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_old_closed_posts(db: aiosqlite.Connection, hours: int = 24):
    """Delete posts that have been closed for longer than `hours`."""
    await db.execute(
        """DELETE FROM lfg_posts
           WHERE status = 'closed'
           AND created_at < datetime('now', ? || ' hours')""",
        (f"-{hours}",),
    )
    await db.commit()


async def get_expired_posts(db: aiosqlite.Connection, hours: int = 24) -> list[dict]:
    async with db.execute(
        """SELECT * FROM lfg_posts
           WHERE status != 'closed'
           AND created_at < datetime('now', ? || ' hours')""",
        (f"-{hours}",),
    ) as cursor:
        return [dict(row) async for row in cursor]
