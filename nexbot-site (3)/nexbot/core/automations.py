"""Automatisations : déclencheur → conditions → actions, choisis dans une liste fermée.

Aucun code fourni par l'utilisateur n'est jamais exécuté. Tout est validé ici,
côté serveur, avant enregistrement ; le bot revalide avant exécution.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from .embeds import check_variables

_ID = r"^\d{15,21}$"

TRIGGERS = {
    "member_join": "Un membre rejoint le serveur",
    "member_leave": "Un membre quitte le serveur",
    "message_contains": "Un message contient un mot-clé",
    "warn_count": "Un membre atteint un nombre d'avertissements",
    "antiraid_alert": "L'anti-raid déclenche une alerte",
}
MEMBER_TRIGGERS = {"member_join", "message_contains", "warn_count"}  # le membre est encore sur le serveur

CONDITIONS = {
    "account_age_below": "Compte créé il y a moins de N jours",
    "has_role": "Le membre a le rôle",
    "lacks_role": "Le membre n'a pas le rôle",
    "in_channel": "Le message est dans le salon",
    "is_bot": "Le membre est un bot",
    "is_not_bot": "Le membre n'est pas un bot",
}
ACTIONS = {
    "send_message": "Envoyer un message dans un salon",
    "dm_member": "Envoyer un message privé au membre",
    "add_role": "Ajouter un rôle",
    "remove_role": "Retirer un rôle",
    "timeout": "Rendre muet (timeout)",
    "delete_message": "Supprimer le message déclencheur",
    "alert": "Envoyer une alerte au salon de sécurité",
}
_NEEDS_MEMBER = {"dm_member", "add_role", "remove_role", "timeout"}


class Trigger(BaseModel):
    type: Literal["member_join", "member_leave", "message_contains", "warn_count", "antiraid_alert"]
    keywords: list[str] = Field(default_factory=list, max_length=20)
    count: int = Field(0, ge=0, le=50)

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v: list[str]) -> list[str]:
        return [k.strip().lower()[:60] for k in v if k and k.strip()]

    @model_validator(mode="after")
    def _params(self):
        if self.type == "message_contains" and not self.keywords:
            raise ValueError("Ajoutez au moins un mot-clé")
        if self.type == "warn_count" and self.count < 1:
            raise ValueError("Indiquez un nombre d'avertissements (1 minimum)")
        return self


class Condition(BaseModel):
    type: Literal["account_age_below", "has_role", "lacks_role", "in_channel", "is_bot", "is_not_bot"]
    days: int = Field(0, ge=0, le=3650)
    role_id: str = ""
    channel_id: str = ""

    @model_validator(mode="after")
    def _params(self):
        import re

        if self.type in {"has_role", "lacks_role"} and not re.match(_ID, self.role_id):
            raise ValueError("Choisissez un rôle pour la condition")
        if self.type == "in_channel" and not re.match(_ID, self.channel_id):
            raise ValueError("Choisissez un salon pour la condition")
        if self.type == "account_age_below" and self.days < 1:
            raise ValueError("Indiquez un nombre de jours (1 minimum)")
        return self


class Action(BaseModel):
    type: Literal["send_message", "dm_member", "add_role", "remove_role", "timeout", "delete_message", "alert"]
    channel_id: str = ""
    role_id: str = ""
    text: str = Field("", max_length=2000)
    minutes: int = Field(0, ge=0, le=40320)
    delay_seconds: int = Field(0, ge=0, le=86400)

    @model_validator(mode="after")
    def _params(self):
        import re

        if self.type == "send_message" and not re.match(_ID, self.channel_id):
            raise ValueError("Choisissez le salon du message")
        if self.type in {"send_message", "dm_member", "alert"} and not self.text.strip():
            raise ValueError("Le message de l'action est vide")
        if self.type in {"add_role", "remove_role"} and not re.match(_ID, self.role_id):
            raise ValueError("Choisissez le rôle de l'action")
        if self.type == "timeout" and not 1 <= self.minutes <= 40320:
            raise ValueError("Durée du timeout : entre 1 minute et 28 jours")
        check_variables(self.text, "welcome" if self.type != "alert" else "generic")
        return self


class AutomationDefinition(BaseModel):
    trigger: Trigger
    conditions: list[Condition] = Field(default_factory=list, max_length=5)
    actions: list[Action] = Field(min_length=1, max_length=5)
    cooldown_seconds: int = Field(10, ge=5, le=86400)

    @model_validator(mode="after")
    def _coherence(self):
        t = self.trigger.type
        for a in self.actions:
            if a.type in _NEEDS_MEMBER and t not in MEMBER_TRIGGERS:
                raise ValueError(f"L'action « {ACTIONS[a.type]} » nécessite un membre présent sur le serveur")
            if a.type == "delete_message" and t != "message_contains":
                raise ValueError("« Supprimer le message » n'est possible qu'avec le déclencheur « message »")
        for c in self.conditions:
            if c.type == "in_channel" and t != "message_contains":
                raise ValueError("La condition de salon n'est possible qu'avec le déclencheur « message »")
            if t == "antiraid_alert" and c.type != "in_channel":
                raise ValueError("Le déclencheur anti-raid n'accepte pas de condition sur un membre")
        return self


class AutomationIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    definition: AutomationDefinition


def validate_automation(data: dict, *, max_actions: int) -> AutomationIn:
    item = AutomationIn.model_validate(data)
    if len(item.definition.actions) > max_actions:
        raise ValueError(f"Votre offre autorise {max_actions} action(s) par automatisation")
    return item


def catalog() -> dict:
    return {"triggers": TRIGGERS, "conditions": CONDITIONS, "actions": ACTIONS,
            "member_triggers": sorted(MEMBER_TRIGGERS), "member_actions": sorted(_NEEDS_MEMBER)}
