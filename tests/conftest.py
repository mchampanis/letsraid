import pytest
import aiosqlite

import db


@pytest.fixture
async def conn():
    """Fresh in-memory SQLite database for each test."""
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await db.init_db(connection)
    yield connection
    await connection.close()


@pytest.fixture
async def sample_lfg(conn):
    """Create a sample LFG post and return its ID."""
    lfg_id = await db.create_lfg(
        conn,
        message_id=100,
        channel_id=200,
        guild_id=300,
        creator_id=1001,
        voice_channel_id=400,
        mode="pvp",
        description="Free kits Stella",
        start_time="now",
        max_slots=3,
    )
    return lfg_id
