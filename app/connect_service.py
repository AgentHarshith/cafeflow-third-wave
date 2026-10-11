"""Connect People: opt-in matching of customers who are in the café right now.

Flow: a signed-up customer asks to connect (button or the Connect agent). Every other
signed-up customer who is online sees a pop-up and may answer the same 3 tap-questions.
When the requester and someone else have both answered, a match % is computed here in
Python and a one-line icebreaker is written (Gemini, with a template fallback). Only a
self-chosen nickname and an optional table number are ever shared — no names, emails or
free text.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DocStore
from .menu_service import CafeError

PROFILE_KIND = "connect_profile"
REQUEST_KIND = "connect_request"
ONLINE_SECONDS = 120
REQUEST_TTL = timedelta(minutes=15)

QUESTIONS: list[dict[str, Any]] = [
    {"id": "coffee", "text": "Your go-to order?", "options": [
        {"id": "black", "label": "Strong & black"},
        {"id": "milky", "label": "Milky latte or cappuccino"},
        {"id": "iced", "label": "Iced & refreshing"},
        {"id": "no_coffee", "label": "Matcha or hot chocolate"},
    ]},
    {"id": "milk", "text": "Milk?", "options": [
        {"id": "regular", "label": "Regular milk"},
        {"id": "plant", "label": "Oat or plant milk"},
        {"id": "none", "label": "No milk"},
        {"id": "any", "label": "Don't mind"},
    ]},
    {"id": "vibe", "text": "Today's vibe?", "options": [
        {"id": "work", "label": "Deep work / study"},
        {"id": "catch_up", "label": "Catching up / meeting"},
        {"id": "chill", "label": "Chilling / reading"},
        {"id": "chat", "label": "Open to chat with anyone"},
    ]},
]
WEIGHTS = {"coffee": 40, "milk": 30, "vibe": 30}
# Partial credit for answers that sit well together (order-independent).
PARTIAL: dict[str, dict[frozenset[str], float]] = {
    "coffee": {frozenset({"black", "iced"}): 0.4, frozenset({"milky", "iced"}): 0.5, frozenset({"milky", "no_coffee"}): 0.4},
    "milk": {frozenset({"regular", "plant"}): 0.3},
    "vibe": {frozenset({"catch_up", "chat"}): 0.8, frozenset({"chill", "chat"}): 0.6, frozenset({"work", "chill"}): 0.4},
}
WILDCARDS = {"milk": ("any", 0.7), "vibe": ("chat", 0.6)}

_LABELS = {q["id"]: {o["id"]: o["label"] for o in q["options"]} for q in QUESTIONS}
_NICKNAME = re.compile(r"^[\w .'\-]{2,24}$", re.UNICODE)

Icebreaker = Callable[[dict[str, str], dict[str, str], list[str]], str | None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def score(a: dict[str, str], b: dict[str, str]) -> tuple[int, list[str]]:
    """Match % from two answer sets, plus the labels both people picked."""
    total, shared = 0.0, []
    for q, weight in WEIGHTS.items():
        x, y = a.get(q), b.get(q)
        if x is None or y is None:
            continue
        if x == y:
            total += weight
            shared.append(_LABELS[q][x])
            continue
        credit = PARTIAL.get(q, {}).get(frozenset({x, y}), 0.0)
        wildcard = WILDCARDS.get(q)
        if wildcard and wildcard[0] in (x, y):
            credit = max(credit, wildcard[1])
        total += weight * credit
    return round(total), shared


def template_icebreaker(shared: list[str], a: dict[str, str], b: dict[str, str]) -> str:
    if shared:
        return f"You both picked \"{shared[0]}\" — ask what they're having today."
    return (f"One of you is \"{_LABELS['coffee'][a['coffee']].lower()}\", the other \"{_LABELS['coffee'][b['coffee']].lower()}\""
            " — swap recommendations!")


class ConnectService:
    def __init__(self, docs: DocStore, icebreaker: Icebreaker | None = None):
        self._docs = docs
        self._icebreaker = icebreaker

    # -- profiles ----------------------------------------------------------------

    def register(self, session_id: str, nickname: str, table: int | None) -> dict[str, Any]:
        nickname = " ".join(nickname.split())
        if not _NICKNAME.match(nickname):
            raise CafeError("Nickname: 2-24 letters, numbers, spaces or . ' -")
        if table is not None and not 1 <= table <= 200:
            raise CafeError("Table number must be between 1 and 200.")
        profile = {"session_id": session_id, "nickname": nickname, "table": table,
                   "joined_at": _now().isoformat(), "last_seen": _now().isoformat()}
        self._docs.put(PROFILE_KIND, session_id, profile)
        return self._public(profile)

    def leave(self, session_id: str) -> None:
        self._docs.delete(PROFILE_KIND, session_id)

    def _profile(self, session_id: str) -> dict[str, Any] | None:
        return self._docs.get(PROFILE_KIND, session_id)

    @staticmethod
    def _public(profile: dict[str, Any]) -> dict[str, Any]:
        return {"nickname": profile["nickname"], "table": profile.get("table")}

    def _online(self) -> dict[str, dict[str, Any]]:
        cutoff = _now() - timedelta(seconds=ONLINE_SECONDS)
        return {p["session_id"]: p for p in self._docs.list(PROFILE_KIND, limit=500)
                if datetime.fromisoformat(p["last_seen"]) >= cutoff}

    # -- requests ------------------------------------------------------------------

    def _open_requests(self) -> list[dict[str, Any]]:
        now = _now()
        return [r for r in self._docs.list(REQUEST_KIND, limit=200)
                if datetime.fromisoformat(r["expires_at"]) > now]

    def start(self, session_id: str) -> dict[str, Any]:
        """Ask everyone online to connect. Returns {needs_signup} or the request summary."""
        profile = self._profile(session_id)
        if profile is None:
            return {"needs_signup": True}
        mine = next((r for r in self._open_requests() if r["requester"] == session_id), None)
        if mine is None:
            mine = {"id": "CR-" + secrets.token_hex(4).upper(), "requester": session_id,
                    "requester_profile": self._public(profile), "created_at": _now().isoformat(),
                    "expires_at": (_now() + REQUEST_TTL).isoformat(), "answers": {}, "declined": [], "matches": {}}
            self._docs.put(REQUEST_KIND, mine["id"], mine)
        others_online = len([s for s in self._online() if s != session_id])
        return {"needs_signup": False, "request_id": mine["id"], "people_notified": others_online,
                "answered": session_id in mine["answers"]}

    def answer(self, session_id: str, request_id: str, answers: dict[str, str]) -> dict[str, Any]:
        profile = self._profile(session_id)
        if profile is None:
            raise CafeError("Sign up to connect first.")
        for q in QUESTIONS:
            if answers.get(q["id"]) not in _LABELS[q["id"]]:
                raise CafeError(f"Please answer: {q['text']}")
        clean = {q["id"]: answers[q["id"]] for q in QUESTIONS}
        request = self._docs.get(REQUEST_KIND, request_id)
        if request is None or datetime.fromisoformat(request["expires_at"]) <= _now():
            raise CafeError("That connect request has expired.")

        # Work out which pairs this answer completes, and write icebreakers *before* taking
        # the row lock (Gemini can take a second or two).
        everyone = {**request["answers"], session_id: clean}
        requester = request["requester"]
        pairs = []
        if requester in everyone:
            others = [s for s in everyone if s != requester] if session_id == requester else [session_id]
            pairs = [s for s in others if s not in request["matches"]]
        prepared: dict[str, dict[str, Any]] = {}
        for other in pairs:
            percent, shared = score(everyone[requester], everyone[other])
            text = None
            if self._icebreaker:
                try:
                    text = self._icebreaker(everyone[requester], everyone[other], shared)
                except Exception:  # noqa: BLE001 - icebreaker is a nice-to-have
                    text = None
            other_profile = self._profile(other) or {"nickname": "someone", "table": None}
            prepared[other] = {
                "id": "M-" + secrets.token_hex(3).upper(), "percent": percent, "shared": shared,
                "icebreaker": text or template_icebreaker(shared, everyone[requester], everyone[other]),
                "icebreaker_by": "gemini" if text else "template",
                "with_profile": self._public(other_profile), "at": _now().isoformat(),
            }

        def save(current: dict[str, Any] | None) -> dict[str, Any]:
            if current is None:
                raise CafeError("That connect request has expired.")
            current["answers"][session_id] = clean
            for other, match in prepared.items():
                current["matches"].setdefault(other, match)
            return current

        self._docs.update(REQUEST_KIND, request_id, save)
        return {"answered": True, "matches_created": len(prepared)}

    def decline(self, session_id: str, request_id: str) -> None:
        def save(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if current is None:
                return None
            current["declined"] = sorted(set(current.get("declined", [])) | {session_id})
            return current

        self._docs.update(REQUEST_KIND, request_id, save)

    # -- polling -------------------------------------------------------------------

    def state(self, session_id: str) -> dict[str, Any]:
        """Everything a browser needs: profile, pop-ups to show, and matches to announce."""
        profile = self._profile(session_id)
        if profile is not None:
            def touch(current: dict[str, Any] | None) -> dict[str, Any] | None:
                if current is None:
                    return None
                current["last_seen"] = _now().isoformat()
                return current

            self._docs.update(PROFILE_KIND, session_id, touch)
        online = self._online()
        invites, mine, matches = [], None, []
        for r in self._open_requests():
            requester = r["requester"]
            if requester == session_id:
                mine = {"request_id": r["id"], "answered": session_id in r["answers"],
                        "responses": len([s for s in r["answers"] if s != session_id])}
                for other, m in r["matches"].items():
                    matches.append({**m, "role": "requester"})
            else:
                if profile and session_id not in r["answers"] and session_id not in r.get("declined", []):
                    invites.append({"request_id": r["id"], "from": r["requester_profile"]})
                if session_id in r["matches"]:
                    m = r["matches"][session_id]
                    matches.append({**m, "with_profile": r["requester_profile"], "role": "invitee"})
        return {
            "signed_up": profile is not None,
            "profile": self._public(profile) if profile else None,
            "people_online": len([s for s in online if s != session_id]),
            "invites": invites,
            "my_request": mine,
            "matches": sorted(matches, key=lambda m: m["at"]),
            "questions": QUESTIONS,
        }
