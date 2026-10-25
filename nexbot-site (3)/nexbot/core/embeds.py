"""Embeds et messages personnalisés : validation (limites Discord) + variables dynamiques.

Partagé entre le Dashboard (validation à l'enregistrement, aperçu) et le bot
(rendu final au moment de l'envoi).
"""
from __future__ import annotations

import re
from typing import Iterable

from pydantic import BaseModel, Field, field_validator, model_validator

# Limites officielles de Discord
LIMIT_CONTENT = 2000
LIMIT_TITLE = 256
LIMIT_DESCRIPTION = 4096
LIMIT_FIELDS = 25
LIMIT_FIELD_NAME = 256
LIMIT_FIELD_VALUE = 1024
LIMIT_FOOTER = 2048
LIMIT_AUTHOR = 256
LIMIT_TOTAL = 6000

VARIABLES: dict[str, str] = {
    "user": "Nom d'affichage du membre",
    "user.name": "Nom d'utilisateur du membre",
    "user.mention": "Mention du membre",
    "user.id": "Identifiant du membre",
    "user.avatar": "URL de l'avatar du membre",
    "user.created": "Date de création du compte",
    "server": "Nom du serveur",
    "server.id": "Identifiant du serveur",
    "server.icon": "URL de l'icône du serveur",
    "member_count": "Nombre de membres",
    "date": "Date du jour",
    "time": "Heure actuelle (UTC)",
    "moderator": "Modérateur (messages de modération)",
    "reason": "Raison de la sanction",
    "duration": "Durée de la sanction",
    "case_id": "Numéro de la sanction",
    "ticket.id": "Numéro du ticket",
    "ticket.category": "Catégorie du ticket",
}

# Variables disponibles selon le contexte d'envoi
CONTEXT_VARIABLES: dict[str, set[str]] = {
    "welcome": {"user", "user.name", "user.mention", "user.id", "user.avatar", "user.created",
                "server", "server.id", "server.icon", "member_count", "date", "time"},
    "moderation": {"user", "user.name", "user.mention", "user.id", "user.avatar", "server", "server.id",
                   "server.icon", "moderator", "reason", "duration", "case_id", "date", "time"},
    "ticket": {"user", "user.name", "user.mention", "user.id", "user.avatar", "server", "server.id",
               "server.icon", "ticket.id", "ticket.category", "date", "time"},
    "announcement": {"server", "server.id", "server.icon", "member_count", "date", "time"},
}
CONTEXT_VARIABLES["generic"] = set(VARIABLES)

_VAR_RE = re.compile(r"\{([a-zA-Z_.]+)\}")
_URL_RE = re.compile(r"^https?://[^\s<>\"]{3,1990}$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class TemplateError(ValueError):
    pass


def find_variables(text: str) -> list[str]:
    return _VAR_RE.findall(text or "")


def check_variables(text: str, context: str = "generic") -> None:
    allowed = CONTEXT_VARIABLES.get(context, CONTEXT_VARIABLES["generic"])
    unknown = sorted({v for v in find_variables(text) if v not in allowed})
    if unknown:
        raise TemplateError("Variable(s) inconnue(s) dans ce contexte : " + ", ".join("{" + v + "}" for v in unknown))


def render_text(text: str, values: dict[str, str]) -> str:
    """Remplace les variables connues ; laisse intactes celles qui n'ont pas de valeur."""
    if not text:
        return text
    return _VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def _url_or_var(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    # Une variable seule (ex. {user.avatar}) est autorisée : elle produira une URL au rendu.
    if _VAR_RE.fullmatch(v):
        return v
    if not _URL_RE.match(v):
        raise ValueError("URL invalide (http:// ou https:// attendu)")
    return v


class EmbedField(BaseModel):
    name: str = Field("", max_length=LIMIT_FIELD_NAME)
    value: str = Field("", max_length=LIMIT_FIELD_VALUE)
    inline: bool = False

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.name.strip() or not self.value.strip():
            raise ValueError("Chaque champ doit avoir un nom et une valeur")
        return self


class EmbedModel(BaseModel):
    title: str = Field("", max_length=LIMIT_TITLE)
    description: str = Field("", max_length=LIMIT_DESCRIPTION)
    url: str = ""
    color: str = "#7C5CFF"
    author_name: str = Field("", max_length=LIMIT_AUTHOR)
    author_icon_url: str = ""
    author_url: str = ""
    thumbnail_url: str = ""
    image_url: str = ""
    footer_text: str = Field("", max_length=LIMIT_FOOTER)
    footer_icon_url: str = ""
    timestamp: bool = False
    fields: list[EmbedField] = Field(default_factory=list, max_length=LIMIT_FIELDS)

    @field_validator("url", "author_icon_url", "author_url", "thumbnail_url", "image_url", "footer_icon_url")
    @classmethod
    def _urls(cls, v: str) -> str:
        return _url_or_var(v)

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        v = (v or "#7C5CFF").strip()
        if not _HEX_RE.match(v):
            raise ValueError("Couleur hexadécimale attendue (#RRGGBB)")
        return v.upper()

    @model_validator(mode="after")
    def _limits(self):
        if self.is_empty():
            raise ValueError("L'embed est vide : ajoutez au moins un titre, une description, un champ ou une image")
        if self.total_length() > LIMIT_TOTAL:
            raise ValueError(f"L'embed dépasse {LIMIT_TOTAL} caractères au total ({self.total_length()})")
        return self

    def is_empty(self) -> bool:
        return not any([self.title.strip(), self.description.strip(), self.fields, self.image_url,
                        self.thumbnail_url, self.author_name.strip(), self.footer_text.strip()])

    def total_length(self) -> int:
        return (len(self.title) + len(self.description) + len(self.author_name) + len(self.footer_text)
                + sum(len(f.name) + len(f.value) for f in self.fields))

    def texts(self) -> Iterable[str]:
        yield from (self.title, self.description, self.author_name, self.footer_text, self.url,
                    self.author_icon_url, self.author_url, self.thumbnail_url, self.image_url, self.footer_icon_url)
        for f in self.fields:
            yield f.name
            yield f.value

    def color_int(self) -> int:
        return int(self.color[1:], 16)


class MessageModel(BaseModel):
    """Message complet : texte simple et/ou embed."""

    content: str = Field("", max_length=LIMIT_CONTENT)
    embed: EmbedModel | None = None

    @model_validator(mode="after")
    def _something(self):
        if not self.content.strip() and self.embed is None:
            raise ValueError("Le message doit contenir du texte ou un embed")
        return self

    def check(self, context: str = "generic") -> "MessageModel":
        check_variables(self.content, context)
        if self.embed:
            for t in self.embed.texts():
                check_variables(t, context)
        return self


def render_embed_dict(embed: EmbedModel, values: dict[str, str]) -> dict:
    """Produit un dictionnaire au format de l'API Discord, tronqué aux limites après rendu."""
    r = lambda s, n=None: (render_text(s, values)[:n] if n else render_text(s, values))  # noqa: E731
    data: dict = {"type": "rich", "color": embed.color_int()}
    if embed.title:
        data["title"] = r(embed.title, LIMIT_TITLE)
    if embed.description:
        data["description"] = r(embed.description, LIMIT_DESCRIPTION)
    url = r(embed.url)
    if url and _URL_RE.match(url):
        data["url"] = url
    if embed.author_name:
        author = {"name": r(embed.author_name, LIMIT_AUTHOR)}
        icon, aurl = r(embed.author_icon_url), r(embed.author_url)
        if icon and _URL_RE.match(icon):
            author["icon_url"] = icon
        if aurl and _URL_RE.match(aurl):
            author["url"] = aurl
        data["author"] = author
    for key, attr in (("thumbnail", "thumbnail_url"), ("image", "image_url")):
        u = r(getattr(embed, attr))
        if u and _URL_RE.match(u):
            data[key] = {"url": u}
    if embed.footer_text:
        footer = {"text": r(embed.footer_text, LIMIT_FOOTER)}
        ficon = r(embed.footer_icon_url)
        if ficon and _URL_RE.match(ficon):
            footer["icon_url"] = ficon
        data["footer"] = footer
    if embed.fields:
        data["fields"] = [
            {"name": r(f.name, LIMIT_FIELD_NAME), "value": r(f.value, LIMIT_FIELD_VALUE), "inline": f.inline}
            for f in embed.fields
        ]
    return data
