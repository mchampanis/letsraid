import db


class TestCreateLfg:
    async def test_returns_id(self, conn):
        lfg_id = await db.create_lfg(
            conn,
            message_id=1, channel_id=2, guild_id=3, creator_id=100,
            voice_channel_id=None, mode="pvp", description=None,
            start_time=None, max_slots=3,
        )
        assert lfg_id is not None
        assert lfg_id > 0

    async def test_creator_is_first_member(self, conn):
        lfg_id = await db.create_lfg(
            conn,
            message_id=1, channel_id=2, guild_id=3, creator_id=100,
            voice_channel_id=None, mode="pve", description="test",
            start_time=None, max_slots=2,
        )
        members = await db.get_lfg_members(conn, lfg_id)
        assert members == [100]

    async def test_optional_fields_nullable(self, conn):
        lfg_id = await db.create_lfg(
            conn,
            message_id=1, channel_id=2, guild_id=3, creator_id=100,
            voice_channel_id=None, mode="pve", description=None,
            start_time=None, max_slots=2,
        )
        post = await db.get_lfg(conn, lfg_id)
        assert post["description"] is None
        assert post["start_time"] is None
        assert post["voice_channel_id"] is None


class TestGetLfg:
    async def test_returns_post(self, conn, sample_lfg):
        post = await db.get_lfg(conn, sample_lfg)
        assert post is not None
        assert post["id"] == sample_lfg
        assert post["mode"] == "pvp"
        assert post["description"] == "Free kits Stella"
        assert post["status"] == "open"

    async def test_returns_none_for_missing(self, conn):
        post = await db.get_lfg(conn, 9999)
        assert post is None


class TestMembers:
    async def test_add_member(self, conn, sample_lfg):
        added = await db.add_member(conn, sample_lfg, 2001)
        assert added is True
        members = await db.get_lfg_members(conn, sample_lfg)
        assert 2001 in members

    async def test_add_duplicate_returns_false(self, conn, sample_lfg):
        await db.add_member(conn, sample_lfg, 2001)
        added = await db.add_member(conn, sample_lfg, 2001)
        assert added is False

    async def test_add_creator_duplicate_returns_false(self, conn, sample_lfg):
        # Creator (1001) is auto-added
        added = await db.add_member(conn, sample_lfg, 1001)
        assert added is False

    async def test_remove_member(self, conn, sample_lfg):
        await db.add_member(conn, sample_lfg, 2001)
        removed = await db.remove_member(conn, sample_lfg, 2001)
        assert removed is True
        members = await db.get_lfg_members(conn, sample_lfg)
        assert 2001 not in members

    async def test_remove_nonmember_returns_false(self, conn, sample_lfg):
        removed = await db.remove_member(conn, sample_lfg, 9999)
        assert removed is False

    async def test_members_ordered_by_join_time(self, conn, sample_lfg):
        await db.add_member(conn, sample_lfg, 2001)
        await db.add_member(conn, sample_lfg, 2002)
        members = await db.get_lfg_members(conn, sample_lfg)
        assert members == [1001, 2001, 2002]

    async def test_add_member_rejected_when_full(self, conn):
        """DB-level slot guard prevents overfilling the party."""
        lfg_id = await db.create_lfg(
            conn,
            message_id=1, channel_id=2, guild_id=3, creator_id=100,
            voice_channel_id=None, mode="pvp", description=None,
            start_time=None, max_slots=2,
        )
        # Creator is member 1, add member 2 to fill it
        await db.add_member(conn, lfg_id, 2001)
        # Member 3 should be rejected
        added = await db.add_member(conn, lfg_id, 2002)
        assert added is False
        members = await db.get_lfg_members(conn, lfg_id)
        assert len(members) == 2


class TestUpdateStatus:
    async def test_update_to_full(self, conn, sample_lfg):
        await db.update_status(conn, sample_lfg, "full")
        post = await db.get_lfg(conn, sample_lfg)
        assert post["status"] == "full"

    async def test_update_to_closed(self, conn, sample_lfg):
        await db.update_status(conn, sample_lfg, "closed")
        post = await db.get_lfg(conn, sample_lfg)
        assert post["status"] == "closed"


class TestUpdateMessageId:
    async def test_updates_message_id(self, conn, sample_lfg):
        await db.update_message_id(conn, sample_lfg, 12345)
        post = await db.get_lfg(conn, sample_lfg)
        assert post["message_id"] == 12345


class TestDeleteLfg:
    async def test_deletes_post(self, conn, sample_lfg):
        await db.delete_lfg(conn, sample_lfg)
        post = await db.get_lfg(conn, sample_lfg)
        assert post is None

    async def test_cascades_members(self, conn, sample_lfg):
        await db.add_member(conn, sample_lfg, 2001)
        await db.delete_lfg(conn, sample_lfg)
        members = await db.get_lfg_members(conn, sample_lfg)
        assert members == []

class TestGetOpenPosts:
    async def test_returns_open_posts(self, conn, sample_lfg):
        posts = await db.get_open_posts(conn, 300)
        assert len(posts) == 1
        assert posts[0]["id"] == sample_lfg

    async def test_excludes_closed(self, conn, sample_lfg):
        await db.update_status(conn, sample_lfg, "closed")
        posts = await db.get_open_posts(conn, 300)
        assert len(posts) == 0

    async def test_includes_full(self, conn, sample_lfg):
        await db.update_status(conn, sample_lfg, "full")
        posts = await db.get_open_posts(conn, 300)
        assert len(posts) == 1

    async def test_filters_by_guild(self, conn, sample_lfg):
        posts = await db.get_open_posts(conn, 999)
        assert len(posts) == 0


class TestBoard:
    async def test_set_and_get(self, conn):
        await db.set_board(conn, 300, 200, 100)
        board = await db.get_board(conn, 300)
        assert board is not None
        assert board["channel_id"] == 200
        assert board["message_id"] == 100

    async def test_get_missing(self, conn):
        board = await db.get_board(conn, 999)
        assert board is None

    async def test_upsert_replaces(self, conn):
        await db.set_board(conn, 300, 200, 100)
        await db.set_board(conn, 300, 201, 101)
        board = await db.get_board(conn, 300)
        assert board["channel_id"] == 201
        assert board["message_id"] == 101


class TestGetExpiredPosts:
    async def test_fresh_post_not_expired(self, conn, sample_lfg):
        expired = await db.get_expired_posts(conn, hours=3)
        assert len(expired) == 0

    async def test_closed_post_not_expired(self, conn, sample_lfg):
        await db.update_status(conn, sample_lfg, "closed")
        # Backdate it
        await conn.execute(
            "UPDATE lfg_posts SET created_at = datetime('now', '-4 hours') WHERE id = ?",
            (sample_lfg,),
        )
        await conn.commit()
        expired = await db.get_expired_posts(conn, hours=3)
        assert len(expired) == 0

    async def test_old_open_post_is_expired(self, conn, sample_lfg):
        await conn.execute(
            "UPDATE lfg_posts SET created_at = datetime('now', '-4 hours') WHERE id = ?",
            (sample_lfg,),
        )
        await conn.commit()
        expired = await db.get_expired_posts(conn, hours=3)
        assert len(expired) == 1
        assert expired[0]["id"] == sample_lfg


class TestDeleteOldClosedPosts:
    async def test_deletes_old_closed(self, conn, sample_lfg):
        await db.update_status(conn, sample_lfg, "closed")
        await conn.execute(
            "UPDATE lfg_posts SET created_at = datetime('now', '-25 hours') WHERE id = ?",
            (sample_lfg,),
        )
        await conn.commit()
        await db.delete_old_closed_posts(conn, hours=24)
        post = await db.get_lfg(conn, sample_lfg)
        assert post is None

    async def test_keeps_recent_closed(self, conn, sample_lfg):
        await db.update_status(conn, sample_lfg, "closed")
        await db.delete_old_closed_posts(conn, hours=24)
        post = await db.get_lfg(conn, sample_lfg)
        assert post is not None

    async def test_keeps_open_posts(self, conn, sample_lfg):
        await conn.execute(
            "UPDATE lfg_posts SET created_at = datetime('now', '-25 hours') WHERE id = ?",
            (sample_lfg,),
        )
        await conn.commit()
        await db.delete_old_closed_posts(conn, hours=24)
        post = await db.get_lfg(conn, sample_lfg)
        assert post is not None
