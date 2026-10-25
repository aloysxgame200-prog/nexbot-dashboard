"""Commandes publiques générales : aide, Dashboard, informations."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ...core.config import VERSION, get_settings, public_dashboard_url
from ...core.premium import FEATURES
from .. import utils
from ..client import Nexbot

HELP_SECTIONS = [
    ("🛡️ Modération", "`/ban` `/unban` `/kick` `/timeout` `/untimeout` `/warn` `/unwarn` `/warnings` `/clear` "
                      "`/history` `/case` `/note add` `/note list` `/slowmode`"),
    ("🚨 Sécurité", "`/raidmode` `/lockdown` `/unlock` `/quarantine` `/unquarantine` `/security`"),
    ("🎫 Communauté", "`/ticket close` `/ticket add` `/ticket remove` `/suggest` `/report` "
                     "· clic droit sur un message → *Applications → Signaler*"),
    ("ℹ️ Infos", "`/dashboard` `/premium` `/serverinfo` `/userinfo` `/avatar` `/botinfo` `/ping` `/invite` `/support`"),
]


def link_view(*buttons: tuple[str, str]) -> discord.ui.View:
    view = discord.ui.View()
    for label, url in buttons:
        view.add_item(discord.ui.Button(label=label, url=url))
    return view


class General(commands.Cog):
    def __init__(self, bot: Nexbot) -> None:
        self.bot = bot

    def _links(self, guild_id: int | None = None) -> discord.ui.View:
        dash = public_dashboard_url() + (f"/g/{guild_id}" if guild_id else "")
        return link_view(("Dashboard", dash), ("Inviter Nexbot", get_settings().bot_invite_url()),
                         ("Support", get_settings().support_url))

    @app_commands.command(name="help", description="Liste des commandes de Nexbot")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = utils.nexbot_embed(self.bot, "Aide de Nexbot",
                                   "Tout se configure depuis le **Dashboard** ; les commandes servent au quotidien.",
                                   await self.bot.embed_color(interaction.guild_id))
        for name, value in HELP_SECTIONS:
            embed.add_field(name=name, value=value, inline=False)
        await interaction.response.send_message(embed=embed, view=self._links(interaction.guild_id), ephemeral=True)

    @app_commands.command(name="dashboard", description="Ouvrir le Dashboard de Nexbot")
    async def dashboard(self, interaction: discord.Interaction) -> None:
        url = public_dashboard_url()
        gid = interaction.guild_id
        embed = utils.nexbot_embed(self.bot, "Dashboard Nexbot",
                                   f"Configurez Nexbot{' pour ce serveur' if gid else ''} en quelques clics :\n{url}"
                                   + (f"/g/{gid}" if gid else ""), await self.bot.embed_color(gid))
        embed.add_field(name="Accès", value="Connexion avec Discord. Il faut la permission « Gérer le serveur ».")
        await interaction.response.send_message(
            embed=embed, view=link_view(("Ouvrir le Dashboard", url + (f"/g/{gid}" if gid else ""))), ephemeral=True)

    @app_commands.command(name="invite", description="Ajouter Nexbot à un serveur")
    async def invite(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=utils.nexbot_embed(self.bot, "Inviter Nexbot", "Merci de faire confiance à Nexbot ! 💜"),
            view=link_view(("Inviter Nexbot", get_settings().bot_invite_url())), ephemeral=True)

    @app_commands.command(name="support", description="Rejoindre le serveur de support")
    async def support(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=utils.nexbot_embed(self.bot, "Support", "Questions, bugs, idées : rejoignez-nous !"),
            view=link_view(("Serveur de support", get_settings().support_url)), ephemeral=True)

    @app_commands.command(name="ping", description="Latence de Nexbot")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=utils.ok(f"Pong ! {round(self.bot.latency * 1000)} ms"),
                                                ephemeral=True)

    @app_commands.command(name="botinfo", description="Informations sur Nexbot")
    async def botinfo(self, interaction: discord.Interaction) -> None:
        embed = utils.nexbot_embed(self.bot, "Nexbot", "Modération, anti-raid, logs, tickets et automatisations.",
                                   await self.bot.embed_color(interaction.guild_id))
        embed.add_field(name="Version", value=VERSION)
        embed.add_field(name="Serveurs", value=str(len(self.bot.guilds)))
        embed.add_field(name="Latence", value=f"{round(self.bot.latency * 1000)} ms")
        embed.add_field(name="En ligne", value=utils.ts(self.bot.started_at, "R"))
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, view=self._links(interaction.guild_id), ephemeral=True)

    @app_commands.command(name="premium", description="Offre Premium de ce serveur")
    @app_commands.guild_only()
    async def premium(self, interaction: discord.Interaction) -> None:
        active = await self.bot.is_premium(interaction.guild_id)  # type: ignore[arg-type]
        embed = utils.nexbot_embed(self.bot, "✦ Nexbot Premium" if active else "Nexbot — offre gratuite",
                                   "Ce serveur est **Premium**. Merci ! 💜" if active else
                                   "Les protections essentielles sont gratuites. Le Premium ajoute la personnalisation "
                                   "avancée :", await self.bot.embed_color(interaction.guild_id))
        if not active:
            perks = [name for name, free, prem in FEATURES if free is False and prem is True]
            embed.add_field(name="Inclus dans le Premium", value="\n".join(f"• {p}" for p in perks))
        await interaction.response.send_message(
            embed=embed, view=link_view(("Gérer le Premium", f"{public_dashboard_url()}/g/{interaction.guild_id}/premium")),
            ephemeral=True)

    @app_commands.command(name="serverinfo", description="Informations sur ce serveur")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        g = interaction.guild
        assert g is not None
        embed = utils.nexbot_embed(self.bot, g.name, None, await self.bot.embed_color(g.id))
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        bots = sum(1 for m in g.members if m.bot)
        embed.add_field(name="Propriétaire", value=f"<@{g.owner_id}>")
        embed.add_field(name="Créé", value=utils.ts(g.created_at, "D"))
        embed.add_field(name="Membres", value=f"{g.member_count} (dont {bots} bots)")
        embed.add_field(name="Salons", value=f"{len(g.text_channels)} textuels · {len(g.voice_channels)} vocaux")
        embed.add_field(name="Rôles", value=str(len(g.roles) - 1))
        embed.add_field(name="Boosts", value=f"{g.premium_subscription_count} (niveau {g.premium_tier})")
        embed.add_field(name="Vérification", value=str(g.verification_level).replace("_", " "))
        embed.set_footer(text=f"ID : {g.id}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Informations sur un membre")
    @app_commands.describe(member="Membre (vous par défaut)")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        m = member or interaction.user
        assert isinstance(m, discord.Member)
        embed = utils.nexbot_embed(self.bot, str(m), None, m.color.value or await self.bot.embed_color(interaction.guild_id))
        embed.set_thumbnail(url=m.display_avatar.url)
        embed.add_field(name="Compte créé", value=utils.ts(m.created_at, "R"))
        embed.add_field(name="A rejoint", value=utils.ts(m.joined_at, "R"))
        roles = [r.mention for r in reversed(m.roles) if not r.is_default()]
        embed.add_field(name=f"Rôles ({len(roles)})", value=" ".join(roles[:20]) or "aucun", inline=False)
        if m.is_timed_out():
            embed.add_field(name="Exclu temporairement jusqu'au", value=utils.ts(m.timed_out_until))
        embed.set_footer(text=f"ID : {m.id}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="Afficher l'avatar d'un utilisateur")
    async def avatar(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        u = user or interaction.user
        embed = utils.nexbot_embed(self.bot, f"Avatar de {u.display_name}", None,
                                   await self.bot.embed_color(interaction.guild_id))
        embed.set_image(url=u.display_avatar.with_size(1024).url)
        await interaction.response.send_message(embed=embed, view=link_view(("Ouvrir", u.display_avatar.url)))


async def setup(bot: Nexbot) -> None:
    await bot.add_cog(General(bot))
