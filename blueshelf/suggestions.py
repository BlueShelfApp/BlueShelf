"""Finds names that look wrong: spelling variants of one person, and titles mistaken for performers.

Everything here only suggests. Merging or removing a name is always the user's decision, because two
similar names can be two real people.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from blueshelf.parser import performer_key


@dataclass
class MergeSuggestion:
    keep_id: Any
    keep_name: str
    keep_count: int
    merge_id: Any
    merge_name: str
    merge_count: int
    reason: str
    score: float

    def pair_key(self) -> list[str]:
        return sorted((performer_key(self.keep_name), performer_key(self.merge_name)))

    def swapped(self) -> "MergeSuggestion":
        return MergeSuggestion(self.merge_id, self.merge_name, self.merge_count,
                               self.keep_id, self.keep_name, self.keep_count, self.reason, self.score)


@dataclass
class StraySuggestion:
    performer_id: int
    name: str
    count: int
    example_title: str
    reason: str


def _match(a: str, b: str) -> tuple[str, float] | None:
    """How two names might be the same person, and how sure that looks."""
    ka, kb = performer_key(a), performer_key(b)
    if not ka or not kb or ka == kb:
        return ("written differently", 1.0) if ka == kb and a != b else None
    # "WankzVR" next to "Wankz" is a VR brand and its flat sibling, not a misspelling.
    shorter, longer = sorted((ka.replace(" ", ""), kb.replace(" ", "")), key=len)
    if longer in (shorter + "vr", "vr" + shorter):
        return None
    wa, wb = ka.split(), kb.split()
    if wa[0] == wb[0]:
        if min(len(wa), len(wb)) == 1 and len(wa) != len(wb):
            return "one is missing the surname", 0.75
        if len(wa) == len(wb) == 2:
            la, lb = wa[-1], wb[-1]
            if (len(la) == 1 and lb.startswith(la)) or (len(lb) == 1 and la.startswith(lb)):
                return "surname shortened to an initial", 0.8
            ratio = SequenceMatcher(None, la, lb).ratio()
            if ratio >= 0.6:
                return "surname spelled differently", 0.5 + ratio / 2
    ratio = SequenceMatcher(None, ka, kb).ratio()
    if ratio >= 0.88:
        return "nearly the same name", ratio
    return None


def merge_suggestions(entries: list[tuple[Any, str, int]],
                      ignored: set[tuple[str, ...]] = frozenset()) -> list[MergeSuggestion]:
    """entries are (id, name, video count). The more-used spelling is kept, best suggestions first."""
    suggestions = []
    for i, first in enumerate(entries):
        for second in entries[i + 1:]:
            found = _match(first[1], second[1])
            if not found:
                continue
            reason, score = found
            keep, merge = (first, second) if (first[2], len(first[1])) >= (second[2], len(second[1])) \
                else (second, first)
            suggestion = MergeSuggestion(keep[0], keep[1], keep[2], merge[0], merge[1], merge[2], reason, score)
            if tuple(suggestion.pair_key()) not in ignored:
                suggestions.append(suggestion)
    suggestions.sort(key=lambda s: (-s.score, s.keep_name.lower()))
    return suggestions


def stray_suggestions(entries: list[tuple[int, str, int, list[str]]],
                      ignored: set[str] = frozenset()) -> list[StraySuggestion]:
    """Performers whose name is part of the title of every video they appear in: probably not a person.

    entries are (performer id, name, video count, titles of those videos).
    """
    out = []
    for performer_id, name, count, titles in entries:
        key = performer_key(name)
        if not titles or count > 2 or not key or performer_key(name) in ignored:
            continue
        if all(key in performer_key(title) for title in titles):
            out.append(StraySuggestion(performer_id, name, count, titles[0], "the name is part of the title"))
    out.sort(key=lambda s: s.name.lower())
    return out
