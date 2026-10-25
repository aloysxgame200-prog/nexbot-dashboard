"""Outils partagés par les modules du bot : durées, hiérarchie, embeds, variables."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from ..core.embeds import EmbedModel, MessageModel, render_embed_dict, render_text

if TYPE_CHECKING:  # pragma: no cover
    from .client import Nexbot

DEFAULT_COLOR = 0x7C5CFF
SUCCESS = 0x34D399
DANGER = 0xF2545B
WARNING = 0xFBBF24
INFO = 0x22D3EE

_DURATION_RE = re.compile(r"(\d+)\s*(secondes?|sec|s|minutes?|min|m|heures?|hours?|h|jours?|days?|j|d|"
                          r"semaines?|sem|weeks?|w)", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "j": 86400, "d": 86400, "w": 604800}


def parse_duration(text: str | None) -> timedelta | None:
    """« 10m », « 2h30m », « 1j », « 1w » → timedelta. None si invalide ou vide."""
    if not text:
        return None
    text = text.strip().lower()
    if text.isdigit():  # un nombre seul = minutes
        return timedelta(minutes=int(text)) if int(text) > 0 else None
    total, pos = 0, 0
    for m in _DURATION_RE.finditer(text):
        if text[pos:m.start()].strip():
            return None
        unit = m.group(2)[0]
        unit = "w" if m.group(2).startswith("sem") else unit
        total += int(m.group(1)) * _UNITS[unit]
        pos = m.end()
    if total <= 0 or text[pos:].strip():
        return None
    return timedelta(seconds=total)


def fmt_duration(delta: timedelta | None) -> str:
    if delta is None:
        return "définitive"
    secs = int(delta.total_seconds())
    parts = []
    for size, name in ((604800, "sem"), (86400, "j"), (3600, "h"), (60, "min"), (1, "s")):
        if secs >= size:
            n, secs = divmod(secs, size)
            parts.append(f"{n} {name}")
    return " ".join(parts[:2]) or "0 s"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime | None, style: str = "f") -> str:
    """Horodatage Discord (affiché dans le fuseau de chaque lecteur)."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def hex_to_int(color: str, default: int = DEFAULT_COLOR) -> int:
    try:
        return int(color.lstrip("#"), 16)
    except (ValueError, AttributeError):
        return default


# ----------------------------------------------------------------------------- hiérarchie des rôles
def hierarchy_error(actor: discord.Member, target: discord.Member | discord.User, me: discord.Member,
                    action: str = "sanctionner") -> str | None:
    """Renvoie un message d'erreur si Discord interdirait l'action, sinon None."""
    guild = me.guild
    if target.id == actor.id:
        return f"Vous ne pouvez pas vous {action} vous-même."
    if target.id == me.id:
        return f"Je ne peux pas me {action} moi-même."
    if not isinstance(target, discord.Member):
        return None  # utilisateur hors du serveur (ex. ban par ID) : pas de hiérarchie
    if target.id == guild.owner_id:
        return f"Impossible de {action} le propriétaire du serveur."
    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        return f"Vous ne pouvez pas {action} {target.mention} : son rôle le plus haut est égal ou supérieur au vôtre."
    if target.top_role >= me.top_role:
        return (f"Je ne peux pas {action} {target.mention} : son rôle est au-dessus du mien. "
                "Placez le rôle de Nexbot plus haut dans les paramètres du serveur.")
    return None


def manageable_roles(member: discord.Member) -> list[discord.Role]:
    """Rôles du membre que le bot peut retirer."""
    me = member.guild.me
    return [r for r in member.roles if not r.is_default() and not r.managed and r < me.top_role]


# ----------------------------------------------------------------------------- variables dynamiques
def template_values(guild: discord.Guild | None, user: discord.abc.User | None = None,
                    extra: dict[str, object] | None = None) -> dict[str, str]:
    """Valeurs des variables {user}, {server}… ; `extra` accepte des clés comme « ticket.id », « reason »."""
    now = utcnow()
    v: dict[str, str] = {"date": now.strftime("%d/%m/%Y"), "time": now.strftime("%H:%M")}
    if guild is not None:
        v.update({"server": guild.name, "server.id": str(guild.id),
                  "server.icon": guild.icon.url if guild.icon else "",
                  "member_count": str(guild.member_count or 0)})
    if user is not None:
        v.update({"user": getattr(user, "display_name", user.name), "user.name": user.name,
                  "user.mention": user.mention, "user.id": str(user.id),
                  "user.avatar": user.display_avatar.url,
                  "user.created": user.created_at.strftime("%d/%m/%Y")})
    for k, val in (extra or {}).items():
        v[k] = str(val)
    return v


def build_embed(model: EmbedModel | dict | None, values: dict[str, str]) -> discord.Embed | None:
    if model is None:
        return None
    if isinstance(model, dict):
        model = EmbedModel.model_validate(model)
    data = render_embed_dict(model, values)
    embed = discord.Embed.from_dict(data)
    if model.timestamp:
        embed.timestamp = utcnow()
    return embed


def build_message(message: MessageModel | dict, values: dict[str, str]) -> tuple[str | None, discord.Embed | None]:
    if isinstance(message, dict):
        message = MessageModel.model_validate(message)
    content = render_text(message.content, values)[:2000] if message.content else None
    return content, build_embed(message.embed, values)


def nexbot_embed(bot: "Nexbot", title: str | None = None, description: str | None = None,
                 color: int | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color if color is not None else DEFAULT_COLOR)
    if bot.user:
        embed.set_footer(text="Nexbot", icon_url=bot.user.display_avatar.url)
    return embed


def ok(text: str) -> discord.Embed:
    return discord.Embed(description=f"✅ {text}", color=SUCCESS)


def err(text: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {text}", color=DANGER)


def warn_embed(text: str) -> discord.Embed:
    return discord.Embed(description=f"⚠️ {text}", color=WARNING)


async def reply(interaction: discord.Interaction, embed: discord.Embed, *, ephemeral: bool = True,
                view: discord.ui.View | None = None) -> None:
    """Répond à une interaction, qu'elle ait déjà été différée ou non."""
    kwargs = {"embed": embed, "ephemeral": ephemeral}
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


def snowflake(value: str | int | None) -> int | None:
    try:
        v = int(str(value).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def chunk_lines(lines: list[str], limit: int = 4000) -> list[str]:
    """Découpe une liste de lignes en blocs respectant la limite des descriptions d'embed."""
    out, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        out.append(cur)
    return out or [""]
