"""Match fighter names across sources.

ESPN writes "Waldo Cortes Acosta" where ufcstats.com writes "Waldo Cortes-Acosta",
"Rongzhu" against "Rong Zhu", and "Jose Miguel Delgado" against "Jose Delgado".
Resolution runs from strict to loose and only accepts a loose match when it is
unambiguous.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable

from ..util import normalise


class NameIndex:
    def __init__(self, names: Iterable[str]) -> None:
        self._display: dict[str, str] = {}
        self._spaceless: dict[str, list[str]] = {}
        self._tokens: dict[str, frozenset[str]] = {}

        for name in names:
            key = normalise(name)
            if not key:
                continue
            self._display.setdefault(key, name)
            self._spaceless.setdefault(key.replace(" ", ""), []).append(key)
            self._tokens[key] = frozenset(key.split())

        self._keys = list(self._display)

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, key: str) -> bool:
        return key in self._display

    def display(self, key: str) -> str:
        return self._display.get(key, key)

    def resolve(self, query: str) -> str | None:
        """The normalised key for a name, or None when nothing matches safely."""
        key = normalise(query)
        if not key:
            return None

        if key in self._display:
            return key

        spaceless = self._spaceless.get(key.replace(" ", ""))
        if spaceless and len(spaceless) == 1:
            return spaceless[0]

        # "Jose Delgado" should find "Jose Miguel Delgado" when only one fighter
        # carries both of those tokens.
        wanted = frozenset(key.split())
        if len(wanted) >= 2:
            subset = [k for k, tokens in self._tokens.items() if wanted <= tokens]
            if len(subset) == 1:
                return subset[0]
            # The reverse: query has more tokens than the stored name.
            superset = [k for k, tokens in self._tokens.items() if tokens <= wanted and len(tokens) >= 2]
            if len(superset) == 1:
                return superset[0]

        close = difflib.get_close_matches(key, self._keys, n=2, cutoff=0.86)
        if len(close) == 1:
            return close[0]
        if len(close) == 2:
            # Accept the top hit only if it is clearly better than the runner-up.
            first = difflib.SequenceMatcher(None, key, close[0]).ratio()
            second = difflib.SequenceMatcher(None, key, close[1]).ratio()
            if first - second > 0.05:
                return close[0]
        return None

    def suggest(self, query: str, limit: int = 5) -> list[str]:
        """Display names that loosely match, for autocomplete and "did you mean" replies.

        Plain matching comes first, and answers almost everything: someone typing
        a name types the start of one of its words. Fuzzy matching is the fallback
        for a genuine misspelling, and is worth avoiding otherwise -- it is tens of
        times slower over a few thousand names, and this runs on every keystroke.
        """
        key = normalise(query)
        if not key:
            return []

        starts: list[str] = []
        contains: list[str] = []
        for stored in self._keys:
            if stored.startswith(key):
                starts.append(stored)
                if len(starts) >= limit:
                    break
            elif len(contains) < limit and key in stored:
                contains.append(stored)

        hits = sorted(starts) + sorted(contains)
        if not hits:
            hits = difflib.get_close_matches(key, self._keys, n=limit, cutoff=0.6)
        if not hits:
            tokens = set(key.split())
            hits = [k for k, t in self._tokens.items() if tokens & t][:limit]
        return [self._display[h] for h in hits[:limit]]
