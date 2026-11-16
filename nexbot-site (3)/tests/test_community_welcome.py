"""Mise à jour du 16/11/2026 : suggestions (filtre, salon automatique, double validation),
signalements plus fiables, messages de départ pendant un raid, /welcome test."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import delete

from nexbot.core import settings_store
from nexbot.core.db import session_scope
from nexbot.core.models import Report, Suggestion

from .conftest import GUILD_A

pytestmark = pytest.mark.usefixtures("clean_guild_data")

SUGG_CHANNEL = 410000000000000001
OTHER_CHANNEL = 410000000000000002


def run(coro):
    return asyncio.run(coro)


def make_bot():
    from nexbot.bot.client import Nexbot

    return Nexbot()


@pytest.fixture(autouse=True)
def _clean_rows():
    def clean():
        with session_scope() as db:
            db.execute(delete(Suggestion))
            db.execute(delete(Report))
    clean()
    yield
    clean()


def save(module: str, data: dict) -> None:
    with session_scope() as db:
        settings_store.save(db, GUILD_A, module, data, user_id=1, premium=False)


def text_channel(cid: int = SUGG_CHANNEL):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = cid
    channel.mention = f"<#{cid}>"
    sent = MagicMock()
    sent.id = 420000000000000001
    sent.channel = channel
    sent.add_reaction = AsyncMock()
    sent.create_thread = AsyncMock()
    channel.send = AsyncMock(return_value=sent)
    return channel


def guild_with(channels: dict):
    return SimpleNamespace(id=GUILD_A, owner_id=1, name="Serveur A", icon=None, member_count=10,
                           get_channel=lambda cid: channels.get(cid))


def member(uid: int, guild, *, staff: bool = False):
    m = MagicMock(spec=discord.Member)
    m.id, m.bot, m.guild, m.roles = uid, False, guild, []
    m.mention = f"<@{uid}>"
    m.guild_permissions = SimpleNamespace(administrator=False, manage_messages=staff, manage_guild=staff)
    m.display_avatar = SimpleNamespace(url="https://cdn/a.png")
    return m


def count_suggestions() -> int:
    with session_scope() as db:
        return db.query(Suggestion).count()


# ----------------------------------------------------------------- suggestions : filtre automatique
def test_suggest_respects_the_automatic_filter():
    async def scenario():
        from nexbot.bot.cogs.community import Community

        save("suggestions", {"enabled": True, "channel_id": str(SUGG_CHANNEL)})
        save("moderation", {"banned_words": ["interdit"]})
        bot = make_bot()
        cog = Community(bot)
        channel = text_channel()
        guild = guild_with({SUGG_CHANNEL: channel})

        msg, error = await cog.publish_suggestion(guild, member(601, guild), "Ajoutez un salon interdit svp")
        assert msg is None and "filtre automatique" in error
        msg, error = await cog.publish_suggestion(guild, member(602, guild), "Rejoignez discord.gg/abcdef")
        assert msg is None and "invitation" in error.lower()
        channel.send.assert_not_awaited()
        assert count_suggestions() == 0

        msg, error = await cog.publish_suggestion(guild, member(603, guild, staff=True), "Un salon interdit aux bots")
        assert msg is not None and error == ""                 # le staff est exempté, comme pour le filtre
        assert count_suggestions() == 1
        await bot.close()
    run(scenario())


def test_suggest_cooldown_and_no_ghost_row_on_send_failure():
    async def scenario():
        from nexbot.bot.cogs.community import Community

        save("suggestions", {"enabled": True, "channel_id": str(SUGG_CHANNEL)})
        bot = make_bot()
        cog = Community(bot)
        channel = text_channel()
        guild = guild_with({SUGG_CHANNEL: channel})
        alice = member(601, guild)

        msg, _ = await cog.publish_suggestion(guild, alice, "Première idée")
        assert msg is not None
        msg, error = await cog.publish_suggestion(guild, alice, "Deuxième idée tout de suite")
        assert msg is None and "Patientez" in error
        cog._suggest_cooldown[(GUILD_A, 601)] = time.monotonic() - 31
        msg, _ = await cog.publish_suggestion(guild, alice, "Deuxième idée plus tard")
        assert msg is not None
        assert count_suggestions() == 2

        channel.send.side_effect = discord.HTTPException(MagicMock(status=403), "Missing Access")
        msg, error = await cog.publish_suggestion(guild, member(602, guild), "Idée impossible à publier")
        assert msg is None and "salon des suggestions" in error
        assert count_suggestions() == 2                         # pas de suggestion fantôme
        assert (GUILD_A, 602) not in cog._suggest_cooldown      # échec : pas de délai imposé
        await bot.close()
    run(scenario())


# ----------------------------------------------------------------- suggestions : salon automatique
def _message(author, channel, content, guild):
    return SimpleNamespace(author=author, channel=channel, content=content, guild=guild, webhook_id=None,
                           type=discord.MessageType.default, delete=AsyncMock())


def test_messages_in_the_suggestion_channel_become_suggestions():
    async def scenario():
        from nexbot.bot.cogs.community import Community

        save("suggestions", {"enabled": True, "channel_id": str(SUGG_CHANNEL), "convert_messages": True})
        save("moderation", {"banned_words": ["interdit"]})
        bot = make_bot()
        cog = Community(bot)
        channel, other = text_channel(), text_channel(OTHER_CHANNEL)
        guild = guild_with({SUGG_CHANNEL: channel, OTHER_CHANNEL: other})

        m = _message(member(601, guild), channel, "Ajouter un salon musique", guild)
        await cog.on_message(m)
        m.delete.assert_awaited_once()
        assert channel.send.await_args.kwargs["embed"].description == "Ajouter un salon musique"
        assert count_suggestions() == 1

        channel.send.reset_mock()
        ignored = [
            _message(member(602, guild), other, "Ailleurs : message normal", guild),     # autre salon
            _message(member(603, guild, staff=True), channel, "Message du staff", guild),  # le staff écrit librement
            _message(member(604, guild), channel, "Un mot interdit ici", guild),          # laissé au filtre
        ]
        for msg in ignored:
            await cog.on_message(msg)
            msg.delete.assert_not_awaited()
        channel.send.assert_not_awaited()

        short = _message(member(605, guild), channel, "ok", guild)
        await cog.on_message(short)
        short.delete.assert_awaited_once()
        assert "entre 5 et 1500" in channel.send.await_args.args[0]   # explication, effacée après 10 s
        assert count_suggestions() == 1

        save("suggestions", {"enabled": True, "channel_id": str(SUGG_CHANNEL), "convert_messages": False})
        bot.invalidate(GUILD_A)
        off = _message(member(606, guild), channel, "Option coupée", guild)
        await cog.on_message(off)
        off.delete.assert_not_awaited()
        await bot.close()
    run(scenario())


def test_convert_messages_option_is_free_and_off_by_default():
    from nexbot.core.schemas import SuggestionSettings, describe_module

    assert SuggestionSettings().convert_messages is False
    field = next(f for f in describe_module("suggestions") if f["name"] == "convert_messages")
    assert field["premium"] is False and field["ui"] == "toggle"


def test_suggestion_cannot_be_reviewed_twice():
    async def scenario():
        from nexbot.bot.cogs.community import Community

        bot = make_bot()
        cog = Community(bot)
        with session_scope() as db:
            row = Suggestion(guild_id=GUILD_A, author_id=601, content="idée", status="accepted")
            db.add(row)
            db.flush()
            sid = row.id
        guild = SimpleNamespace(id=GUILD_A, get_member=lambda _: None, name="Serveur A")
        response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), edit_message=AsyncMock(),
                                   send_modal=AsyncMock())
        interaction = SimpleNamespace(guild_id=GUILD_A, guild=guild, response=response, message=None,
                                      user=SimpleNamespace(id=1, mention="<@1>"))
        await cog.apply_review(interaction, sid, False, "")
        assert "déjà été traitée" in response.send_message.await_args.kwargs["embed"].description
        with session_scope() as db:
            assert db.get(Suggestion, sid).status == "accepted"   # le premier choix est conservé

        staff = MagicMock(spec=discord.Member)
        staff.guild = SimpleNamespace(id=GUILD_A)
        staff.guild_permissions = SimpleNamespace(manage_guild=True)
        staff.roles = []
        interaction.user = staff
        response.send_message.reset_mock()
        await cog.review_suggestion(interaction, sid, True)
        response.send_modal.assert_not_awaited()                  # le formulaire ne s'ouvre même pas
        assert "déjà été traitée" in response.send_message.await_args.kwargs["embed"].description
        await bot.close()
    run(scenario())


# ----------------------------------------------------------------- signalements
def test_report_checks_happen_before_the_form_and_links_stay_on_the_server():
    async def scenario():
        from nexbot.bot.cogs.community import Community

        save("reports", {"enabled": True, "channel_id": str(SUGG_CHANNEL)})
        bot = make_bot()
        cog = Community(bot)
        channel = text_channel()
        guild = guild_with({SUGG_CHANNEL: channel})
        reporter = SimpleNamespace(id=601, mention="<@601>")

        def interaction():
            response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), send_modal=AsyncMock())
            return SimpleNamespace(guild=guild, guild_id=GUILD_A, user=reporter, response=response)

        bot_msg = SimpleNamespace(author=SimpleNamespace(id=999, bot=True), content="x", jump_url="")
        it = interaction()
        await cog.report_message(it, bot_msg)
        it.response.send_modal.assert_not_awaited()               # pas de formulaire inutile
        assert "ne pouvez pas signaler" in it.response.send_message.await_args.kwargs["embed"].description

        own_msg = SimpleNamespace(author=SimpleNamespace(id=601, bot=False), content="x", jump_url="")
        it = interaction()
        await cog.report_message(it, own_msg)
        it.response.send_modal.assert_not_awaited()

        ok_msg = SimpleNamespace(author=SimpleNamespace(id=602, bot=False), content="x", jump_url="")
        it = interaction()
        await cog.report_message(it, ok_msg)
        it.response.send_modal.assert_awaited_once()

        target = SimpleNamespace(id=602, bot=False, mention="<@602>")
        it = interaction()
        await cog.report.callback(cog, it, target, "insultes",
                                  "https://discord.com/channels/123456789012345678/1/2")
        assert "autre serveur" in it.response.send_message.await_args.kwargs["embed"].description
        channel.send.assert_not_awaited()
        with session_scope() as db:
            assert db.query(Report).count() == 0

        it = interaction()
        await cog.report.callback(cog, it, target, "insultes", f"https://discord.com/channels/{GUILD_A}/1/2")
        channel.send.assert_awaited_once()
        with session_scope() as db:
            assert db.query(Report).count() == 1
        await bot.close()
    run(scenario())


def test_report_button_answers_even_without_embed():
    async def scenario():
        from nexbot.bot.cogs.community import Community

        bot = make_bot()
        cog = Community(bot)
        with session_scope() as db:
            r = Report(guild_id=GUILD_A, reporter_id=1, target_id=2, reason="x")
            db.add(r)
            db.flush()
            rid = r.id
        staff = MagicMock(spec=discord.Member)
        staff.id = 7
        staff.guild = SimpleNamespace(id=GUILD_A)
        staff.roles = []
        bot.is_moderator = AsyncMock(return_value=True)
        response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(user=staff, response=response, message=SimpleNamespace(embeds=[]))
        await cog.update_report(interaction, rid, "handled")
        response.send_message.assert_awaited_once()                # avant : « Échec de l'interaction »
        with session_scope() as db:
            assert db.get(Report, rid).status == "handled"
        await bot.close()
    run(scenario())


# ----------------------------------------------------------------- bienvenue / départ
def _welcome_member(guild):
    return SimpleNamespace(id=601, bot=False, guild=guild, name="alice", display_name="Alice", mention="<@601>",
                           display_avatar=SimpleNamespace(url="https://cdn/a.png"),
                           created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))


def test_no_leave_messages_during_a_raid():
    async def scenario():
        from nexbot.bot.cogs.welcome import Welcome

        save("welcome", {"leave_enabled": True, "leave_channel_id": str(SUGG_CHANNEL)})
        bot = make_bot()
        cog = Welcome(bot)
        cog._send = AsyncMock()
        guild = guild_with({})
        raid = {"on": True}
        bot.get_cog = lambda name: SimpleNamespace(raid_active=lambda gid: raid["on"]) if name == "AntiRaid" else None

        await cog.on_member_remove(_welcome_member(guild))
        cog._send.assert_not_awaited()                             # raid : pas de rafale de messages
        raid["on"] = False
        await cog.on_member_remove(_welcome_member(guild))
        cog._send.assert_awaited_once()
        await bot.close()
    run(scenario())


def test_welcome_diagnose_and_render():
    from nexbot.bot.cogs.welcome import Welcome
    from nexbot.core.schemas import WelcomeSettings

    channel = MagicMock(spec=discord.TextChannel)
    channel.mention = "<#1>"
    perms = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
    channel.permissions_for = lambda me: perms
    guild = SimpleNamespace(id=GUILD_A, me=object(), name="Serveur A", icon=None, member_count=42,
                            get_channel=lambda cid: channel if cid == SUGG_CHANNEL else None)

    conf = WelcomeSettings()
    problems = Welcome.diagnose(guild, conf, "welcome")
    assert any("désactivé" in p for p in problems) and any("Aucun salon" in p for p in problems)

    conf = WelcomeSettings(enabled=True, channel_id=str(SUGG_CHANNEL))
    assert Welcome.diagnose(guild, conf, "welcome") == []
    perms.send_messages = False
    assert any("ne peut pas écrire" in p for p in Welcome.diagnose(guild, conf, "welcome"))

    conf = WelcomeSettings(leave_enabled=True, leave_channel_id="410000000000000099")
    assert any("n'existe plus" in p for p in Welcome.diagnose(guild, conf, "leave"))
    assert any("Premium" in p for p in Welcome.diagnose(guild, WelcomeSettings(), "dm"))

    content, embed = Welcome.render(guild, _welcome_member(guild), WelcomeSettings().text, None, True)
    assert content == "Bienvenue <@601> sur **Serveur A** ! Nous sommes maintenant 42." and embed is None


def test_welcome_test_command_is_registered_for_managers_only():
    from nexbot.bot.cogs.welcome import Welcome

    group = Welcome.welcome_group
    assert group.name == "welcome" and group.guild_only
    assert group.default_permissions.manage_guild
    assert "test" in {c.name for c in group.commands}
