"""Tests du bot sans connexion à Discord (objets simulés)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexbot.bot import utils
from nexbot.bot.automod_logic import SlidingCounter, SpamDetector, banned_word_hit, check_content, domain_allowed
from nexbot.core.db import session_scope
from nexbot.core.schemas import ModerationSettings
from nexbot.core.security import OWNER_ID

from .conftest import GUILD_A

pytestmark = pytest.mark.usefixtures("clean_guild_data")


def run(coro):
    return asyncio.run(coro)


def make_bot():
    from nexbot.bot.client import Nexbot

    return Nexbot()


# ----------------------------------------------------------------- commandes * : silence total
def fake_message(author_id: int, content: str):
    return SimpleNamespace(author=SimpleNamespace(id=author_id, bot=False), content=content,
                           channel=MagicMock(), guild=None)


@pytest.mark.parametrize("content", ["*help", "*stats", "*shutdown", "*maintenance on", "*blacklist user 1"])
def test_star_commands_are_ignored_for_everyone_but_the_owner(content):
    async def scenario():
        bot = make_bot()
        bot.process_commands = AsyncMock()
        msg = fake_message(123456789012345678, content)
        await bot.on_message(msg)
        bot.process_commands.assert_not_awaited()          # jamais traité
        msg.channel.send.assert_not_called()               # aucune réponse
        msg.channel.add_reaction = MagicMock()
        await bot.on_message(fake_message(OWNER_ID, content))
        bot.process_commands.assert_awaited_once()          # le propriétaire, lui, est servi
        await bot.close()
    run(scenario())


def test_global_check_and_cog_check_refuse_non_owner():
    async def scenario():
        from nexbot.bot.cogs.owner import Owner

        bot = make_bot()
        intruder = SimpleNamespace(author=SimpleNamespace(id=OWNER_ID + 1))
        owner = SimpleNamespace(author=SimpleNamespace(id=OWNER_ID))
        assert all(check(intruder) is False for check in bot._checks)
        assert all(check(owner) is True for check in bot._checks)
        cog = Owner(bot)
        assert await cog.cog_check(intruder) is False and await cog.cog_check(owner) is True
        # Même une erreur de commande reste muette pour un non-propriétaire
        ctx = SimpleNamespace(author=SimpleNamespace(id=1), reply=AsyncMock(), command=None, guild=None)
        await bot.on_command_error(ctx, Exception("x"))
        ctx.reply.assert_not_awaited()
        await bot.close()
    run(scenario())


def test_owner_id_cannot_be_overridden_by_name_or_role():
    from nexbot.core.security import is_owner

    assert not is_owner("598080343958093831 ")
    assert not is_owner(SimpleNamespace(id=OWNER_ID))  # un objet n'est jamais accepté, seul l'ID compte


# ----------------------------------------------------------------- filtre automatique
def conf(**kw):
    return ModerationSettings(**kw)


def test_automod_rules():
    c = conf(filter_links=True, banned_words=["arnaque", "free nitro"], max_mentions=3)
    assert check_content("rejoins discord.gg/abcd", c, 0).rule == "invite"
    assert check_content("regarde https://youtube.com/watch?v=1", c, 0) is None        # domaine autorisé
    assert check_content("regarde https://evil.example/x", c, 0).rule == "link"
    assert check_content("c'est une ÂRN4QUE !", c, 0).rule == "word"                     # accents + leet
    assert check_content("FREE   nitro ici", c, 0).rule == "word"
    assert check_content("arnaques", conf(banned_words=["arnaque"]), 0).rule == "word"    # pluriel bloqué
    assert check_content("a r n a q u e", conf(banned_words=["arnaque"]), 0).rule == "word"  # espaces ajoutés
    assert check_content("un bon conseil", conf(banned_words=["con"]), 0) is None         # mot court : mot entier
    assert check_content("salut", c, 4).rule == "mentions"
    caps = conf(caps_filter=True, caps_percent=70)
    assert check_content("ARRÊTEZ DE CRIER SUR CE SERVEUR", caps, 0).rule == "caps"
    assert check_content("Bonjour à tous, bonne soirée", caps, 0) is None
    assert domain_allowed("www.music.youtube.com", ["youtube.com"]) and not domain_allowed("youtube.com.evil.io", ["youtube.com"])
    assert banned_word_hit("rien à signaler", ["arnaque"]) is None


def test_spam_and_duplicate_detection():
    d = SpamDetector()
    c = conf(spam_max_messages=5, spam_window_seconds=5, duplicate_max=3)
    verdicts = [d.check(1, 1, f"msg {i}", c, now=100 + i * 0.5) for i in range(6)]
    assert verdicts[:5] == [None] * 5 and verdicts[5].rule == "spam"
    d2 = SpamDetector()
    out = [d2.check(1, 2, "achetez maintenant", c, now=200 + i * 10) for i in range(3)]
    assert out[2].rule == "duplicate"
    assert d2.check(1, 3, "achetez maintenant", c, now=230) is None  # un autre membre n'est pas pénalisé


def test_sliding_counter_window():
    s = SlidingCounter()
    assert [s.hit("g", 10, now=t) for t in (0, 1, 2)] == [1, 2, 3]
    assert s.hit("g", 10, now=15) == 1  # les anciens événements sortent de la fenêtre


# ----------------------------------------------------------------- utilitaires
@pytest.mark.parametrize("text,seconds", [("10m", 600), ("2h30m", 9000), ("1j", 86400), ("1w", 604800),
                                          ("15", 900), ("3 jours", 259200), ("", None), ("abc", None), ("10x", None)])
def test_parse_duration(text, seconds):
    d = utils.parse_duration(text)
    assert (d.total_seconds() if d else None) == seconds


def test_hierarchy_rules():
    guild = SimpleNamespace(owner_id=1)
    me = SimpleNamespace(id=99, top_role=10, guild=guild)
    import discord

    def member(i, role):
        m = MagicMock(spec=discord.Member)
        m.id, m.top_role, m.mention = i, role, f"<@{i}>"
        return m
    mod = member(2, 5)
    assert utils.hierarchy_error(mod, member(1, 1), me) is not None          # propriétaire du serveur
    assert utils.hierarchy_error(mod, member(3, 5), me) is not None          # rôle égal au modérateur
    assert utils.hierarchy_error(mod, member(3, 4), me) is None
    assert utils.hierarchy_error(member(1, 1), member(3, 12), me) is not None  # au-dessus du bot
    assert utils.hierarchy_error(mod, mod, me) is not None                   # soi-même


def test_template_values_and_render():
    guild = SimpleNamespace(name="Nexus", id=5, icon=None, member_count=42)
    user = SimpleNamespace(display_name="Alice", name="alice", mention="<@7>", id=7,
                           display_avatar=SimpleNamespace(url="https://cdn/a.png"),
                           created_at=datetime(2021, 3, 12, tzinfo=timezone.utc))
    values = utils.template_values(guild, user, {"ticket.id": 9})
    content, embed = utils.build_message({"content": "Salut {user.mention} sur {server} ({member_count})",
                                          "embed": {"title": "Ticket {ticket.id}", "thumbnail_url": "{user.avatar}"}},
                                         values)
    assert content == "Salut <@7> sur Nexus (42)"
    assert embed.title == "Ticket 9" and embed.thumbnail.url == "https://cdn/a.png"


# ----------------------------------------------------------------- sanctions & escalade
def test_warn_escalates_to_timeout_and_is_recorded():
    async def scenario():
        from nexbot.bot.cogs.moderation import Moderation
        from nexbot.core import settings_store
        from nexbot.core.models import Sanction

        with session_scope() as db:
            settings_store.save(db, GUILD_A, "moderation", {"warns_to_timeout": 2, "timeout_minutes": 30},
                                user_id=1, premium=False)
        bot = make_bot()
        cog = Moderation(bot)
        me = SimpleNamespace(id=999, top_role=10, mention="<@999>", name="Nexbot",
                             display_avatar=SimpleNamespace(url="https://cdn/b.png"))
        target = SimpleNamespace(id=555, top_role=1, mention="<@555>", name="bob", display_name="Bob", bot=False,
                                 display_avatar=SimpleNamespace(url="https://cdn/c.png"),
                                 created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                                 send=AsyncMock(), timeout=AsyncMock(), kick=AsyncMock(),
                                 guild_permissions=SimpleNamespace(administrator=False))
        guild = SimpleNamespace(id=GUILD_A, owner_id=1, me=me, name="Serveur A", icon=None, member_count=10,
                                get_member=lambda uid: target if uid == 555 else None)
        mod = SimpleNamespace(id=777, mention="<@777>", name="mod", display_avatar=me.display_avatar)
        case1, dm1 = await cog.sanction(guild, target, mod, "warn", "spam")
        target.timeout.assert_not_awaited()
        case2, _ = await cog.sanction(guild, target, mod, "warn", "spam encore")
        target.timeout.assert_awaited_once()                    # 2e avertissement → exclusion automatique
        assert target.timeout.await_args.args[0] == timedelta(minutes=30)
        assert dm1 is True and target.send.await_count >= 2     # membre prévenu en MP
        with session_scope() as db:
            kinds = [s.type for s in db.query(Sanction).filter_by(guild_id=GUILD_A, user_id=555).order_by(Sanction.id)]
        assert kinds == ["warn", "warn", "timeout"]
        assert await cog.active_warns(GUILD_A, 555) == 2
        await bot.close()
    run(scenario())


def test_sanction_refused_on_higher_role_and_record_cancelled():
    async def scenario():
        import discord

        from nexbot.bot.cogs.moderation import Moderation, ModerationError
        from nexbot.core.models import Sanction

        bot = make_bot()
        cog = Moderation(bot)
        me = SimpleNamespace(id=999, top_role=10)
        target = SimpleNamespace(id=556, top_role=3, name="eve", display_name="Eve", mention="<@556>", send=AsyncMock(),
                                 display_avatar=SimpleNamespace(url="https://cdn/e.png"),
                                 created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                                 kick=AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no")))
        guild = SimpleNamespace(id=GUILD_A, owner_id=1, me=me, name="Serveur A", icon=None, member_count=3,
                                get_member=lambda uid: target)
        mod = SimpleNamespace(id=777)
        with pytest.raises(ModerationError):
            await cog.sanction(guild, target, mod, "kick", "x")
        with session_scope() as db:
            assert db.query(Sanction).filter_by(user_id=556).count() == 0  # aucune trace d'une sanction échouée
        await bot.close()
    run(scenario())


# ----------------------------------------------------------------- automatisations
def test_automation_trigger_and_conditions():
    from nexbot.bot.cogs.automations import Automations
    from nexbot.core.automations import AutomationDefinition

    d = AutomationDefinition.model_validate({"trigger": {"type": "message_contains", "keywords": ["Free Nitro"]},
                                             "actions": [{"type": "delete_message"}]})
    msg = SimpleNamespace(content="Clique ici pour du FREE NITRO", channel=SimpleNamespace(id=1))
    assert Automations._trigger_matches(d, "message_contains", msg, None)
    assert not Automations._trigger_matches(d, "member_join", msg, None)
    young = SimpleNamespace(created_at=datetime.now(timezone.utc) - timedelta(days=1), roles=[], bot=False)
    cond = SimpleNamespace(type="account_age_below", days=3, role_id="", channel_id="")
    assert Automations._condition(cond, young, None)
    assert not Automations._condition(SimpleNamespace(type="is_bot"), young, None)


def test_automation_rate_limit_and_cooldown():
    async def scenario():
        from nexbot.bot.cogs.automations import MAX_RUNS_PER_MINUTE, Automations
        from nexbot.core.automations import AutomationDefinition

        bot = make_bot()
        cog = Automations(bot)
        cog._run = AsyncMock()
        d = AutomationDefinition.model_validate({"trigger": {"type": "member_join"},
                                                 "actions": [{"type": "alert", "text": "x"}], "cooldown_seconds": 5})
        cog._cache[GUILD_A] = [(1, "a", d)]
        guild = SimpleNamespace(id=GUILD_A)
        m = SimpleNamespace(id=1, created_at=datetime.now(timezone.utc), roles=[], bot=False)
        assert await cog.fire(guild, "member_join", member=m) == 1
        assert await cog.fire(guild, "member_join", member=m) == 0       # délai minimum entre deux exécutions
        total = sum([await cog.fire(guild, "member_join", member=SimpleNamespace(
            id=100 + i, created_at=m.created_at, roles=[], bot=False)) for i in range(MAX_RUNS_PER_MINUTE + 10)])
        assert total == MAX_RUNS_PER_MINUTE - 1                             # plafond par minute et par serveur
        await asyncio.sleep(0)
        await bot.close()
    run(scenario())


# ----------------------------------------------------------------- jobs du Dashboard & statistiques
def test_send_message_job_requires_premium():
    async def scenario():
        from nexbot.bot.cogs.core import Core

        bot = make_bot()
        core = Core(bot)
        guild = SimpleNamespace(id=GUILD_A, get_channel=lambda _: None)
        ok, text = await core.job_send_message({"guild": guild, "payload": {"channel_id": "1", "message": {"content": "x"}}})
        assert ok is False and "Premium" in text
        await bot.close()
    run(scenario())


def test_usage_stats_flush_and_owner_numbers():
    async def scenario():
        from nexbot.bot.cogs.core import Core
        from nexbot.bot.cogs.owner import Owner

        bot = make_bot()
        core, owner = Core(bot), Owner(bot)
        for uid in (1, 2, 3, 2):
            bot.record_usage(uid, "ban")
        bot.record_usage(4, "help")
        bot.stat(GUILD_A, "messages", 7)
        await core._flush()
        bot.record_usage(1, "ban")       # déjà compté aujourd'hui : pas de doublon
        await core._flush()
        n = await bot.db(owner._usage_numbers)
        assert n["dau"] == 4 and n["mau"] == 4
        assert n["cmd_day"] == 6 and n["top"][0][0] == "ban"
        await bot.close()
    run(scenario())


# ----------------------------------------------------------------- anti-raid
class FakeRole:
    def __init__(self, rid: int, position: int, managed: bool = False, default: bool = False):
        self.id, self.position, self.managed, self._default = rid, position, managed, default

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < (other.position if isinstance(other, FakeRole) else other)

    def __ge__(self, other):
        return not self < other


def fake_guild(members: dict, top: int = 10):
    me = SimpleNamespace(id=999, top_role=FakeRole(9, top),
                         guild_permissions=SimpleNamespace(manage_guild=True, view_audit_log=True, manage_webhooks=True))
    return SimpleNamespace(id=GUILD_A, owner_id=1, me=me, owner=None, name="Serveur A", icon=None, member_count=50,
                           get_member=members.get, get_channel=lambda _: None, get_role=lambda _: None)


def fake_member(uid: int, guild=None, position: int = 1):
    return SimpleNamespace(id=uid, bot=False, guild=guild, top_role=FakeRole(100 + uid, position),
                           roles=[FakeRole(1, 0, default=True), FakeRole(200 + uid, position)],
                           created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                           kick=AsyncMock(), timeout=AsyncMock(), remove_roles=AsyncMock(), edit=AsyncMock(),
                           guild_permissions=SimpleNamespace(administrator=False), mention=f"<@{uid}>")


def test_join_raid_detected_and_all_raiders_handled():
    async def scenario():
        from nexbot.bot.cogs.antiraid import AntiRaid
        from nexbot.core import settings_store
        from nexbot.core.models import SecurityIncident

        with session_scope() as db:
            settings_store.save(db, GUILD_A, "antiraid", {"join_threshold": 3, "join_window_seconds": 10,
                                                          "join_raid_action": "kick"}, user_id=1, premium=False)
        bot = make_bot()
        cog = AntiRaid(bot)
        members: dict = {}
        guild = fake_guild(members)
        raiders = [fake_member(10 + i, guild) for i in range(4)]
        for m in raiders:
            members[m.id] = m
        for m in raiders:
            await cog.on_member_join(m)
        assert cog.raid_active(GUILD_A)
        assert all(m.kick.await_count == 1 for m in raiders)   # les 3 premiers + celui arrivé pendant le raid
        with session_scope() as db:
            inc = db.query(SecurityIncident).filter_by(guild_id=GUILD_A, kind="join_raid").one()
            assert inc.severity == "high" and inc.details["arrivées"] == 3
        await bot.close()
    run(scenario())


def test_nuke_detection_strips_roles_of_attacker_but_ignores_trusted():
    async def scenario():
        import discord

        from nexbot.bot.cogs.antiraid import AntiRaid
        from nexbot.core import settings_store
        from nexbot.core.models import SecurityIncident

        with session_scope() as db:
            settings_store.save(db, GUILD_A, "antiraid", {"channel_delete_threshold": 3, "action_window_seconds": 30,
                                                          "nuke_action": "strip_roles",
                                                          "trusted_user_ids": ["300000000000000077"]},
                                user_id=1, premium=False)
        bot = make_bot()
        cog = AntiRaid(bot)
        members: dict = {}
        guild = fake_guild(members)
        attacker = fake_member(42, guild, position=5)
        trusted = fake_member(300000000000000077, guild, position=5)
        members.update({42: attacker, trusted.id: trusted})

        def entry(user_id, n):
            return SimpleNamespace(guild=guild, user_id=user_id, action=discord.AuditLogAction.channel_delete,
                                   target=SimpleNamespace(id=n), before=None, after=None)
        for i in range(3):
            await cog.on_audit_log_entry_create(entry(trusted.id, i))
        trusted.remove_roles.assert_not_awaited()                   # utilisateur de confiance : ignoré
        for i in range(2):
            await cog.on_audit_log_entry_create(entry(42, i))
        attacker.remove_roles.assert_not_awaited()                  # sous le seuil : aucune réaction
        await cog.on_audit_log_entry_create(entry(42, 3))
        attacker.remove_roles.assert_awaited_once()                 # seuil atteint : rôles retirés
        attacker.timeout.assert_awaited_once()
        with session_scope() as db:
            inc = db.query(SecurityIncident).filter_by(guild_id=GUILD_A, kind="channel_delete").one()
            assert inc.actor_id == 42 and inc.severity == "critical" and "rôle" in inc.action_taken
        await bot.close()
    run(scenario())


def test_ban_reaction_requires_explicit_confirmation():
    from nexbot.core.schemas import AntiRaidSettings

    with pytest.raises(Exception):
        AntiRaidSettings(nuke_action="ban")
    assert AntiRaidSettings(nuke_action="ban", confirm_destructive=True).nuke_action == "ban"
