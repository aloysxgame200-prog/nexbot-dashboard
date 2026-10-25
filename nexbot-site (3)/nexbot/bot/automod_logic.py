"""Logique pure du filtre automatique et des détecteurs à fenêtre glissante (testable sans Discord)."""
from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass

INVITE_RE = re.compile(r"(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg|discord\.me)/[\w-]+", re.IGNORECASE)
URL_RE = re.compile(r"https?://([^\s/<>\"']+)", re.IGNORECASE)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "€": "e"})


def normalize(text: str) -> str:
    """Minuscules, sans accents ni caractères invisibles, « leet speak » simplifié."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c) and unicodedata.category(c) != "Cf")
    return text.translate(_LEET)


def domain_allowed(domain: str, allowed: list[str]) -> bool:
    domain = domain.lower().split(":")[0].removeprefix("www.")
    for a in allowed:
        a = a.lower().strip().removeprefix("www.")
        if a and (domain == a or domain.endswith("." + a)):
            return True
    return False


def banned_word_hit(text: str, words: list[str]) -> str | None:
    norm = re.sub(r"\s+", " ", normalize(text))
    squashed = re.sub(r"[\s._\-*]+", "", norm)
    for word in words:
        w = re.sub(r"\s+", " ", normalize(word)).strip()
        if not w:
            continue
        if " " in w:
            if w in norm:
                return word
        elif re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", norm) or (len(w) >= 5 and w in squashed):
            return word
    return None


def caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 12:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


@dataclass
class Verdict:
    rule: str
    reason: str


def check_content(content: str, conf, mention_count: int) -> Verdict | None:
    """Règles qui ne dépendent que du message (pas de l'historique)."""
    if conf.filter_invites and INVITE_RE.search(content):
        return Verdict("invite", "Invitation Discord non autorisée")
    if conf.filter_links:
        for domain in URL_RE.findall(content):
            if not domain_allowed(domain, conf.allowed_domains):
                return Verdict("link", f"Lien non autorisé ({domain[:60]})")
    if conf.banned_words:
        hit = banned_word_hit(content, conf.banned_words)
        if hit:
            return Verdict("word", "Mot interdit")
    if conf.max_mentions and mention_count > conf.max_mentions:
        return Verdict("mentions", f"Trop de mentions ({mention_count})")
    if getattr(conf, "caps_filter", False) and caps_ratio(content) * 100 >= conf.caps_percent:
        return Verdict("caps", "Abus de majuscules")
    return None


class SlidingCounter:
    """Compte des événements par clé sur une fenêtre de temps glissante."""

    def __init__(self, maxlen: int = 500) -> None:
        self._events: dict[object, deque] = defaultdict(lambda: deque(maxlen=maxlen))

    def hit(self, key: object, window: float, now: float | None = None, value: object = None) -> int:
        now = time.monotonic() if now is None else now
        q = self._events[key]
        q.append((now, value))
        while q and now - q[0][0] > window:
            q.popleft()
        return len(q)

    def values(self, key: object) -> list:
        return [v for _, v in self._events.get(key, ())]

    def reset(self, key: object) -> None:
        self._events.pop(key, None)

    def prune(self, older_than: float = 3600, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for key in [k for k, q in self._events.items() if not q or now - q[-1][0] > older_than]:
            self._events.pop(key, None)


class SpamDetector:
    """Rafales de messages et messages identiques répétés, par membre et par serveur."""

    def __init__(self) -> None:
        self.rate = SlidingCounter(100)
        self.recent: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))

    def check(self, guild_id: int, user_id: int, content: str, conf, now: float | None = None) -> Verdict | None:
        now = time.monotonic() if now is None else now
        key = (guild_id, user_id)
        if conf.anti_spam:
            n = self.rate.hit(key, conf.spam_window_seconds, now)
            if n > conf.spam_max_messages:
                self.rate.reset(key)
                return Verdict("spam", f"Spam ({n} messages en {conf.spam_window_seconds} s)")
        text = normalize(content).strip()
        if text and conf.duplicate_max:
            q = self.recent[key]
            q.append((now, text))
            same = sum(1 for t, c in q if c == text and now - t <= 120)
            if same >= conf.duplicate_max:
                q.clear()
                return Verdict("duplicate", f"Message répété {same} fois")
        return None
