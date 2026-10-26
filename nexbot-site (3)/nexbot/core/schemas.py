"""Schéma des paramètres de chaque module.

Chaque champ porte des métadonnées d'interface (`ui`, `label`, `help`) et de
plan (`premium`, `free_max`). Le Dashboard génère ses formulaires à partir de
ces définitions et le bot lit les mêmes modèles : ajouter une option Premium =
ajouter un champ ici, sans réécrire le projet.

Les identifiants Discord sont stockés en chaînes : JavaScript ne représente pas
exactement les entiers de 64 bits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator

from .embeds import EmbedModel, check_variables

_SNOWFLAKE = re.compile(r"^\d{15,21}$")


def F(default: Any, label: str, *, ui: str | None = None, premium: bool = False, help: str = "",
      free_max: int | None = None, context: str | None = None, options: list | None = None,
      section: str | None = None, **kw) -> Any:
    extra: dict[str, Any] = {"label": label}
    if ui:
        extra["ui"] = ui
    if premium:
        extra["premium"] = True
    if help:
        extra["help"] = help
    if free_max is not None:
        extra["free_max"] = free_max
    if context:
        extra["context"] = context
    if options:
        extra["options"] = options
    if section:
        extra["section"] = section
    if isinstance(default, (list, dict)):
        return Field(default_factory=lambda d=default: type(d)(d), json_schema_extra=extra, **kw)
    return Field(default, json_schema_extra=extra, **kw)


def _snowflake(v: Any) -> str:
    if v is None or v == "":
        return ""
    v = str(v).strip()
    if not _SNOWFLAKE.match(v):
        raise ValueError("Identifiant Discord invalide")
    return v


class ModuleModel(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_ids(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for name, field in cls.model_fields.items():
            ui = (field.json_schema_extra or {}).get("ui") if isinstance(field.json_schema_extra, dict) else None
            if name not in out:
                continue
            if ui in {"channel", "category", "role", "voice"}:
                out[name] = _snowflake(out[name])
            elif ui in {"channels", "roles", "users"}:
                vals = out[name] or []
                if isinstance(vals, str):
                    vals = [x for x in re.split(r"[\s,]+", vals) if x]
                out[name] = list(dict.fromkeys(_snowflake(x) for x in vals if str(x).strip()))
            elif ui == "words":
                vals = out[name] or []
                if isinstance(vals, str):
                    vals = vals.split("\n")
                out[name] = list(dict.fromkeys(w.strip()[:100] for w in vals if str(w).strip()))
        return out

    @model_validator(mode="after")
    def _templates(self):
        for name, field in type(self).model_fields.items():
            extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
            ctx = extra.get("context")
            if not ctx:
                continue
            value = getattr(self, name)
            if isinstance(value, str):
                check_variables(value, ctx)
            elif isinstance(value, EmbedModel):
                for t in value.texts():
                    check_variables(t, ctx)
        return self




# --------------------------------------------------------------------------- général
class GeneralSettings(ModuleModel):
    language: Literal["fr", "en"] = F("fr", "Langue des réponses", ui="select",
                                      options=[["fr", "Français"], ["en", "English"]])
    embed_color: str = F("#7C5CFF", "Couleur des embeds de Nexbot", ui="color", pattern=r"^#[0-9a-fA-F]{6}$")
    mod_role_ids: list[str] = F([], "Rôles modérateurs",
                                ui="roles", help="Rôles autorisés à utiliser les commandes de modération, en plus des permissions Discord.")


# --------------------------------------------------------------------------- modération
class ModerationSettings(ModuleModel):
    enabled: bool = F(True, "Module de modération actif", section="Général")
    dm_on_sanction: bool = F(True, "Prévenir le membre en message privé", section="Général")
    require_reason: bool = F(False, "Raison obligatoire pour chaque sanction", section="Général")
    warn_expiry_days: int = F(0, "Expiration des avertissements (jours, 0 = jamais)", section="Général", ge=0, le=365)

    escalation_enabled: bool = F(True, "Sanctions progressives automatiques", section="Escalade")
    warns_to_timeout: int = F(3, "Avertissements avant timeout (0 = désactivé)", section="Escalade", ge=0, le=50)
    timeout_minutes: int = F(60, "Durée du timeout automatique (minutes)", section="Escalade", ge=1, le=40320)
    warns_to_kick: int = F(5, "Avertissements avant expulsion (0 = désactivé)", section="Escalade", ge=0, le=50)
    warns_to_ban: int = F(0, "Avertissements avant bannissement (0 = désactivé)", section="Escalade", ge=0, le=50)

    automod_enabled: bool = F(True, "Filtre automatique actif", section="Filtres")
    filter_invites: bool = F(True, "Bloquer les invitations Discord", section="Filtres")
    filter_links: bool = F(False, "Bloquer les liens", section="Filtres")
    allowed_domains: list[str] = F(["youtube.com", "youtu.be", "tenor.com"], "Domaines autorisés", ui="words",
                                   section="Filtres", help="Un domaine par ligne.")
    banned_words: list[str] = F([], "Mots interdits", ui="words", section="Filtres", free_max=50,
                                help="Un mot ou une expression par ligne. 50 en gratuit, 1000 en Premium.",
                                max_length=1000)
    filter_edits: bool = F(True, "Vérifier aussi les messages modifiés", section="Filtres",
                           help="Empêche de contourner le filtre en modifiant un message après l'avoir envoyé.")
    automod_action: Literal["delete", "warn", "timeout"] = F(
        "delete", "Action sur un message filtré", ui="select", section="Filtres",
        options=[["delete", "Supprimer"], ["warn", "Supprimer + avertir"], ["timeout", "Supprimer + timeout 10 min"]])

    anti_spam: bool = F(True, "Protection anti-spam", section="Anti-spam")
    spam_max_messages: int = F(6, "Messages maximum…", section="Anti-spam", ge=2, le=50)
    spam_window_seconds: int = F(5, "…sur une fenêtre de (secondes)", section="Anti-spam", ge=1, le=60)
    duplicate_max: int = F(4, "Messages identiques répétés maximum", section="Anti-spam", ge=2, le=20)
    max_mentions: int = F(6, "Mentions maximum par message", section="Anti-spam", ge=1, le=50)

    exempt_role_ids: list[str] = F([], "Rôles exemptés des filtres", ui="roles", section="Exclusions")
    exempt_channel_ids: list[str] = F([], "Salons exemptés des filtres", ui="channels", section="Exclusions")

    caps_filter: bool = F(False, "Filtre des majuscules excessives", premium=True, section="Avancé (Premium)")
    caps_percent: int = F(70, "Pourcentage de majuscules toléré", premium=True, section="Avancé (Premium)", ge=30, le=100)
    msg_warn: str = F("{user.mention}, tu as reçu un avertissement sur **{server}** : {reason}",
                      "Message d'avertissement", ui="template", context="moderation", premium=True,
                      section="Messages (Premium)", max_length=1500)
    msg_timeout: str = F("Tu as été rendu muet sur **{server}** pendant {duration}. Raison : {reason}",
                         "Message de timeout", ui="template", context="moderation", premium=True,
                         section="Messages (Premium)", max_length=1500)
    msg_kick: str = F("Tu as été expulsé de **{server}**. Raison : {reason}", "Message d'expulsion",
                      ui="template", context="moderation", premium=True, section="Messages (Premium)", max_length=1500)
    msg_ban: str = F("Tu as été banni de **{server}**. Raison : {reason}", "Message de bannissement",
                     ui="template", context="moderation", premium=True, section="Messages (Premium)", max_length=1500)


# --------------------------------------------------------------------------- anti-raid
_RAID_ACTIONS = [["alert", "Alerter uniquement"], ["quarantine", "Mettre en quarantaine"],
                 ["kick", "Expulser"], ["lockdown", "Verrouillage (niveau de vérification max)"]]


class AntiRaidSettings(ModuleModel):
    enabled: bool = F(True, "Anti-raid actif", section="Général")
    alert_channel_id: str = F("", "Salon des alertes de sécurité", ui="channel", section="Général")
    alert_role_id: str = F("", "Rôle à mentionner lors d'une alerte", ui="role", section="Général")
    quarantine_role_id: str = F("", "Rôle de quarantaine", ui="role", section="Général",
                                help="Rôle sans accès aux salons. Nexbot retire les autres rôles du membre et lui donne celui-ci.")
    trusted_user_ids: list[str] = F([], "Utilisateurs de confiance (ignorés par l'anti-nuke)", ui="users",
                                    section="Général", help="Identifiants Discord, un par ligne.")

    join_protection: bool = F(True, "Détection des arrivées massives", section="Arrivées")
    join_threshold: int = F(8, "Arrivées maximum…", section="Arrivées", ge=3, le=200)
    join_window_seconds: int = F(10, "…sur une fenêtre de (secondes)", section="Arrivées", ge=3, le=600)
    join_raid_action: Literal["alert", "quarantine", "kick", "lockdown"] = F(
        "quarantine", "Réaction à une arrivée massive", ui="select", options=_RAID_ACTIONS, section="Arrivées")
    raid_mode_minutes: int = F(10, "Durée du mode protection renforcée (minutes)", section="Arrivées", ge=1, le=1440)
    min_account_age_days: int = F(0, "Âge minimum des comptes (jours, 0 = désactivé)", section="Arrivées", ge=0, le=365)
    young_account_action: Literal["alert", "quarantine", "kick"] = F(
        "alert", "Réaction à un compte trop récent", ui="select", options=_RAID_ACTIONS[:3], section="Arrivées")
    bot_add_protection: bool = F(True, "Expulser les bots ajoutés par un membre non autorisé", section="Arrivées",
                                 help="Seuls le propriétaire du serveur et les utilisateurs de confiance peuvent ajouter des bots.")

    nuke_protection: bool = F(True, "Protection anti-nuke", section="Anti-nuke")
    action_window_seconds: int = F(30, "Fenêtre de détection (secondes)", section="Anti-nuke", ge=5, le=600)
    channel_delete_threshold: int = F(3, "Suppressions de salons", section="Anti-nuke", ge=1, le=50)
    channel_create_threshold: int = F(6, "Créations de salons", section="Anti-nuke", ge=1, le=100)
    role_delete_threshold: int = F(3, "Suppressions de rôles", section="Anti-nuke", ge=1, le=50)
    role_create_threshold: int = F(6, "Créations de rôles", section="Anti-nuke", ge=1, le=100)
    ban_threshold: int = F(5, "Bannissements / expulsions", section="Anti-nuke", ge=1, le=100)
    webhook_threshold: int = F(3, "Créations de webhooks", section="Anti-nuke", ge=1, le=50)
    permission_monitor: bool = F(True, "Alerter sur les permissions dangereuses ajoutées", section="Anti-nuke")
    nuke_action: Literal["alert", "strip_roles", "ban"] = F(
        "strip_roles", "Réaction contre l'auteur", ui="select", section="Anti-nuke",
        options=[["alert", "Alerter uniquement"], ["strip_roles", "Retirer ses rôles"], ["ban", "Bannir"]])
    confirm_destructive: bool = F(False, "Je confirme les actions destructrices (bannissement automatique)",
                                  section="Anti-nuke",
                                  help="Obligatoire pour choisir « Bannir » : protège contre les faux positifs.")

    mention_raid_threshold: int = F(0, "Spam de mentions coordonné : membres distincts (0 = désactivé)",
                                    premium=True, section="Avancé (Premium)", ge=0, le=50)
    escalation_strikes: int = F(0, "Récidives avant bannissement automatique (0 = désactivé)",
                                premium=True, section="Avancé (Premium)", ge=0, le=10)
    alert_dm_owner: bool = F(False, "Alerter le propriétaire du serveur en message privé", premium=True,
                             section="Avancé (Premium)")
    auto_restore: bool = F(False, "Recréer les salons et rôles supprimés lors d'un nuke", premium=True,
                           section="Avancé (Premium)",
                           help="Nexbot recrée la structure (nom, type, position, permissions). Les messages supprimés ne peuvent pas être restaurés.")

    @model_validator(mode="after")
    def _confirm(self):
        if self.nuke_action == "ban" and not self.confirm_destructive:
            raise ValueError("Cochez la confirmation des actions destructrices pour activer le bannissement automatique")
        return self


# --------------------------------------------------------------------------- logs
LOG_CATEGORIES: list[tuple[str, str]] = [
    ("members", "Arrivées et départs"),
    ("messages", "Messages supprimés et modifiés"),
    ("moderation", "Sanctions et actions des modérateurs"),
    ("roles", "Rôles des membres"),
    ("channels", "Salons créés / supprimés"),
    ("permissions", "Changements de permissions"),
    ("antiraid", "Anti-raid"),
    ("security", "Incidents de sécurité"),
    ("errors", "Erreurs de Nexbot"),
]
_DEFAULT_LOG_COLORS = {"members": "#3BA55C", "messages": "#FAA61A", "moderation": "#ED4245", "roles": "#5865F2",
                       "channels": "#00B0F4", "permissions": "#EB459E", "antiraid": "#ED4245",
                       "security": "#ED4245", "errors": "#99AAB5"}

_log_fields: dict[str, Any] = {
    "retention_days": (int, F(30, "Conservation des données en base (jours)", free_max=30, section="Général",
                              help="30 jours en gratuit, jusqu'à 365 en Premium.", ge=1, le=365)),
    "ignored_channel_ids": (list[str], F([], "Salons ignorés (logs de messages)", ui="channels", section="Général")),
    "show_avatars": (bool, F(True, "Afficher l'avatar des membres", premium=True, section="Apparence (Premium)")),
}
for _key, _label in LOG_CATEGORIES:
    _log_fields[f"{_key}_enabled"] = (bool, F(False, _label, section="Catégories"))
    _log_fields[f"{_key}_channel_id"] = (str, F("", f"Salon — {_label}", ui="channel", section="Catégories"))
for _key, _label in LOG_CATEGORIES:
    _log_fields[f"{_key}_color"] = (str, F(_DEFAULT_LOG_COLORS[_key], f"Couleur — {_label}", ui="color",
                                           premium=True, section="Apparence (Premium)",
                                           pattern=r"^#[0-9a-fA-F]{6}$"))

LogSettings = create_model("LogSettings", __base__=ModuleModel, **_log_fields)


# --------------------------------------------------------------------------- bienvenue
class WelcomeSettings(ModuleModel):
    enabled: bool = F(False, "Message de bienvenue", section="Bienvenue")
    channel_id: str = F("", "Salon de bienvenue", ui="channel", section="Bienvenue")
    mention_member: bool = F(True, "Mentionner le membre", section="Bienvenue")
    text: str = F("Bienvenue {user.mention} sur **{server}** ! Nous sommes maintenant {member_count}.",
                  "Texte", ui="template", context="welcome", section="Bienvenue", max_length=2000)
    embed: EmbedModel | None = F(None, "Embed de bienvenue", ui="embed", context="welcome", premium=True,
                                 section="Bienvenue")
    dm_enabled: bool = F(False, "Envoyer aussi un message privé", premium=True, section="Bienvenue")
    dm_text: str = F("Bienvenue sur {server} ! Pense à lire le règlement.", "Message privé", ui="template",
                     context="welcome", premium=True, section="Bienvenue", max_length=2000)

    leave_enabled: bool = F(False, "Message de départ", section="Départ")
    leave_channel_id: str = F("", "Salon de départ", ui="channel", section="Départ")
    leave_text: str = F("**{user.name}** a quitté le serveur. Nous sommes maintenant {member_count}.",
                        "Texte de départ", ui="template", context="welcome", section="Départ", max_length=2000)
    leave_embed: EmbedModel | None = F(None, "Embed de départ", ui="embed", context="welcome", premium=True,
                                       section="Départ")


# --------------------------------------------------------------------------- rôles automatiques
class AutoRoleSettings(ModuleModel):
    enabled: bool = F(False, "Rôles automatiques actifs")
    role_ids: list[str] = F([], "Rôles donnés aux nouveaux membres", ui="roles", free_max=2, max_length=10,
                            help="2 rôles en gratuit, 10 en Premium.")
    bot_role_ids: list[str] = F([], "Rôles donnés aux bots", ui="roles", max_length=5)
    skip_quarantined: bool = F(True, "Ne pas donner les rôles aux membres en quarantaine")
    delay_seconds: int = F(0, "Délai avant attribution (secondes)", premium=True, ge=0, le=3600)


# --------------------------------------------------------------------------- tickets
class TicketSettings(ModuleModel):
    enabled: bool = F(False, "Tickets actifs", section="Général")
    category_id: str = F("", "Catégorie où créer les tickets", ui="category", section="Général")
    staff_role_ids: list[str] = F([], "Rôles du staff (accès aux tickets)", ui="roles", section="Général")
    log_channel_id: str = F("", "Salon des logs de tickets", ui="channel", section="Général")
    max_open_per_user: int = F(1, "Tickets ouverts maximum par membre", section="Général", ge=1, le=5)
    panel_title: str = F("Support", "Titre du panneau", section="Panneau", max_length=256)
    panel_description: str = F("Clique sur le bouton ci-dessous pour ouvrir un ticket privé avec le staff.",
                               "Description du panneau", ui="textarea", section="Panneau", max_length=2000)
    button_label: str = F("Ouvrir un ticket", "Texte du bouton", section="Panneau", max_length=80)
    open_text: str = F("Bonjour {user.mention}, un membre du staff va te répondre. Décris ta demande ici.",
                       "Message d'ouverture", ui="template", context="ticket", section="Messages", max_length=2000)
    close_text: str = F("Ticket #{ticket.id} fermé. Merci d'avoir contacté le staff de {server} !",
                        "Message de fermeture (MP au membre)", ui="template", context="ticket",
                        section="Messages", max_length=2000)
    categories: list[str] = F([], "Catégories de tickets (menu de choix)", ui="words", premium=True,
                              section="Avancé (Premium)", max_length=10, help="Une catégorie par ligne, 10 maximum.")
    button_style: Literal["primary", "secondary", "success", "danger"] = F(
        "primary", "Couleur du bouton", ui="select", premium=True, section="Avancé (Premium)",
        options=[["primary", "Bleu"], ["secondary", "Gris"], ["success", "Vert"], ["danger", "Rouge"]])
    panel_embed: EmbedModel | None = F(None, "Embed du panneau (remplace titre et description)", ui="embed",
                                       context="announcement", premium=True, section="Avancé (Premium)")
    auto_close_hours: int = F(0, "Fermeture auto après inactivité (heures, 0 = jamais)", premium=True,
                              section="Avancé (Premium)", ge=0, le=720)


# --------------------------------------------------------------------------- suggestions / signalements
class SuggestionSettings(ModuleModel):
    enabled: bool = F(False, "Suggestions actives")
    channel_id: str = F("", "Salon des suggestions", ui="channel")
    reviewer_role_ids: list[str] = F([], "Rôles pouvant accepter / refuser", ui="roles")
    create_threads: bool = F(False, "Créer un fil de discussion par suggestion", premium=True)


class ReportSettings(ModuleModel):
    enabled: bool = F(False, "Signalements actifs")
    channel_id: str = F("", "Salon privé de réception des signalements", ui="channel")
    staff_role_ids: list[str] = F([], "Rôles à mentionner", ui="roles")
    ping_staff: bool = F(True, "Mentionner le staff à chaque signalement")


# --------------------------------------------------------------------------- Premium : thème & stats
class ThemeSettings(ModuleModel):
    primary: str = F("#7C5CFF", "Couleur principale", ui="color", premium=True, pattern=r"^#[0-9a-fA-F]{6}$")
    secondary: str = F("#22D3EE", "Couleur secondaire", ui="color", premium=True, pattern=r"^#[0-9a-fA-F]{6}$")
    accent: str = F("#F472B6", "Couleur d'accentuation", ui="color", premium=True, pattern=r"^#[0-9a-fA-F]{6}$")
    background: str = F("#0B0B12", "Fond", ui="color", premium=True, pattern=r"^#[0-9a-fA-F]{6}$")
    surface: str = F("#15151F", "Surfaces et menus", ui="color", premium=True, pattern=r"^#[0-9a-fA-F]{6}$")
    radius: int = F(14, "Arrondi des éléments (px)", premium=True, ge=0, le=24)
    density: Literal["comfortable", "compact"] = F("comfortable", "Densité d'affichage", ui="select", premium=True,
                                                   options=[["comfortable", "Confortable"], ["compact", "Compacte"]])

    @field_validator("primary", "secondary", "accent", "background", "surface")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


STAT_METRICS: list[tuple[str, str]] = [
    ("joins", "Arrivées"), ("leaves", "Départs"), ("messages", "Messages"),
    ("sanctions", "Sanctions"), ("automod", "Messages filtrés"), ("incidents", "Incidents de sécurité"),
    ("tickets", "Tickets ouverts"),
]


class StatsSettings(ModuleModel):
    default_period: Literal["7", "30", "90"] = F("7", "Période par défaut (jours)", ui="select", premium=True,
                                                options=[["7", "7 jours"], ["30", "30 jours"], ["90", "90 jours"]])
    visible_metrics: list[str] = F(["joins", "leaves", "messages", "sanctions"], "Statistiques affichées",
                                   ui="multiselect", premium=True, options=[list(m) for m in STAT_METRICS])
    chart_style: Literal["bars", "line"] = F("bars", "Type de graphique", ui="select", premium=True,
                                             options=[["bars", "Barres"], ["line", "Courbe"]])

    @field_validator("visible_metrics")
    @classmethod
    def _metrics(cls, v: list[str]) -> list[str]:
        allowed = {m for m, _ in STAT_METRICS}
        return [m for m in dict.fromkeys(v) if m in allowed]


# --------------------------------------------------------------------------- registre
@dataclass(frozen=True)
class ModuleSpec:
    key: str
    model: type[ModuleModel]
    title: str
    icon: str
    description: str
    premium_only: bool = False


MODULES: dict[str, ModuleSpec] = {m.key: m for m in [
    ModuleSpec("general", GeneralSettings, "Général", "settings", "Langue, couleur et rôles modérateurs."),
    ModuleSpec("moderation", ModerationSettings, "Modération", "gavel",
               "Sanctions, escalade automatique, filtres et anti-spam."),
    ModuleSpec("antiraid", AntiRaidSettings, "Anti-raid", "shield",
               "Arrivées massives, anti-nuke, quarantaine et alertes."),
    ModuleSpec("logs", LogSettings, "Logs", "scroll", "Salons de logs par catégorie et conservation."),
    ModuleSpec("welcome", WelcomeSettings, "Bienvenue & départ", "wave", "Messages d'arrivée et de départ."),
    ModuleSpec("autoroles", AutoRoleSettings, "Rôles automatiques", "tag", "Rôles donnés aux nouveaux membres."),
    ModuleSpec("tickets", TicketSettings, "Tickets", "ticket", "Panneau, catégories et messages des tickets."),
    ModuleSpec("suggestions", SuggestionSettings, "Suggestions", "bulb", "Salon et votes des suggestions."),
    ModuleSpec("reports", ReportSettings, "Signalements", "flag", "Réception des signalements des membres."),
    ModuleSpec("theme", ThemeSettings, "Thème du Dashboard", "palette", "Couleurs et affichage du Dashboard.",
               premium_only=True),
    ModuleSpec("stats", StatsSettings, "Statistiques", "chart", "Préférences des statistiques.", premium_only=True),
]}


def field_meta(model: type[BaseModel], name: str) -> dict[str, Any]:
    extra = model.model_fields[name].json_schema_extra
    return dict(extra) if isinstance(extra, dict) else {}


def describe_module(key: str) -> list[dict[str, Any]]:
    """Description sérialisable d'un module pour le générateur de formulaires du Dashboard."""
    spec = MODULES[key]
    out = []
    for name, field in spec.model.model_fields.items():
        meta = field_meta(spec.model, name)
        ann = field.annotation
        ui = meta.get("ui")
        if ui is None:
            ui = "toggle" if ann is bool else "number" if ann is int else "text"
        item: dict[str, Any] = {"name": name, "ui": ui, "label": meta.get("label", name),
                                "help": meta.get("help", ""), "premium": bool(meta.get("premium")),
                                "section": meta.get("section", ""), "context": meta.get("context")}
        if "options" in meta:
            item["options"] = meta["options"]
        if "free_max" in meta:
            item["free_max"] = meta["free_max"]
        for c in field.metadata:
            for attr in ("ge", "le", "max_length"):
                if getattr(c, attr, None) is not None:
                    item[{"ge": "min", "le": "max", "max_length": "maxlength"}[attr]] = getattr(c, attr)
        out.append(item)
    return out
