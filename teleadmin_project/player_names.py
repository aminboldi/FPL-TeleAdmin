"""Keep the database's Persian player names authoritative in every output.

Every source we publish revolves around the same limited set of Premier League
players, and the database already holds the Persian spelling the channel uses
for each one. A translation model does not: it transliterates from the English
spelling each time it meets a name, so the same player arrives as زولیس in one
post and تزولیس in the next, and is sometimes left in English entirely.

Three passes make the stored spelling win, in increasing order of cost:

1. ``prompt_glossary`` lists the players actually named in the source text,
   with their Persian spelling, inside the translation prompt. This is what
   prevents the wrong transliteration from being produced at all, and it costs
   a handful of lines rather than a glossary of every player.
2. ``enforce`` rewrites the result deterministically: an English name becomes
   its Persian spelling, and any recorded variant becomes the canonical one.
3. When a player is known to be in the source but the translation contains
   neither their Persian name nor a recorded variant, a near-identical Persian
   word is taken as a new variant of that name, corrected, and stored in the
   player's ``alias`` column — so pass 2 catches it from then on and the
   operator can see and edit it in the /players editor.

Nothing here is specific to a player. Adding a name or fixing a transliteration
is a database edit, never a code or prompt change.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Rebuilding the index compiles a pattern over every stored spelling, which
# takes long enough to be worth keeping off a request. Every path that changes
# a name calls ``reload``, so this interval is only a safety net for a database
# edited from outside the bot.
_CACHE_SECONDS = 3600
# A ceiling on what one player can accumulate, so a broken model response can
# never grow the alias column without bound.
_MAX_LEARNED_VARIANTS = 12
_MIN_NAME_CHARS = 3
_PROMPT_GLOSSARY_LIMIT = 40

# Escapes rather than literals throughout: several of these characters are
# invisible or combining, and a literal one cannot be reviewed in a diff.
_PERSIAN_RANGE = "\u0600-\u06ff"
_PERSIAN_WORD_RE = re.compile(rf"[{_PERSIAN_RANGE}]{{{_MIN_NAME_CHARS},}}")
_HTML_SPLIT_RE = re.compile(r"(<[^>]*>)")
_DIACRITICS_RE = re.compile(r"[\u064b-\u0652\u0670\u0640]")
# Persian writes compounds with a zero-width non-joiner: invisible, but it
# changes the bytes. It is folded away for comparison and allowed anywhere
# inside a match.
_ZWNJ = "\u200c"
_JOINERS = "\u200c\u200d\u200e\u200f"

# Arabic-script spellings that mean the same Persian letter. Folding these away
# is what lets one stored spelling match text written with any of them.
_FOLD_MAP = str.maketrans(
    {
        "ي": "ی", "ى": "ی", "ئ": "ی",  # ي ى ئ -> ی
        "ك": "ک",                                          # ك -> ک
        "ة": "ه", "ۀ": "ه",                      # ة ۀ -> ه
        "أ": "ا", "إ": "ا",                      # أ إ -> ا
        "آ": "ا", "ٱ": "ا",                      # آ ٱ -> ا
        "ؤ": "و",                                          # ؤ -> و
        "ء": "",                                                # ء
    }
    | {joiner: "" for joiner in _JOINERS}
)
# The reverse view, used to build patterns that match unfolded text.
_EQUIVALENT_LETTERS = {
    "ی": "یيىئ", "ک": "کك", "ه": "هةۀ", "ا": "اآأإٱ", "و": "وؤ",
}
# Letters a transliterator swaps for one another when reading a Latin spelling:
# Tzolis' ز against a ذ, Cherki's ش against a چ. A substitution outside these
# groups is not a spelling variant of the same name, it is a different word.
_CONFUSABLE_GROUPS = ("تط", "سصث", "زذضظ", "چجشژ", "حهخ", "قغکگ", "بپ")
_CONFUSABLE = {
    letter: group for group in _CONFUSABLE_GROUPS for letter in group
}

_PROMPT_HEADER = (
    "Player names — these Persian spellings are the channel's own and are "
    "authoritative. Use each one exactly as written, everywhere that player is "
    "named, and never transliterate the English name yourself:"
)


@dataclass(frozen=True)
class Player:
    """One player's names, as the rest of the module needs them."""

    id: int
    canonical: str
    display: str
    canonical_fa: str
    display_fa: str
    aliases: tuple[str, ...]
    team: str

    @property
    def persian(self) -> str:
        """The spelling every output should end up using."""
        return self.display_fa or self.canonical_fa


@dataclass(frozen=True)
class _Index:
    players: tuple[Player, ...]
    pattern: re.Pattern | None
    # spelling key -> (replacement, the match must start with a capital)
    replacements: dict[str, tuple[str, bool]]
    # spelling key -> the player it names, for spellings that name only one
    owners: dict[str, Player]
    # every Persian spelling already claimed by some player
    claimed_persian: frozenset[str]


_EMPTY_INDEX = _Index((), None, {}, {}, frozenset())
_index: _Index | None = None
_index_loaded_at = 0.0
_lock = threading.Lock()


def fold(text: str) -> str:
    """Collapse interchangeable Arabic-script spellings of a Persian string."""
    return _DIACRITICS_RE.sub("", str(text or "")).translate(_FOLD_MAP)


def _strip_accents(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", str(text or ""))
        if not unicodedata.combining(character)
    )


def lookup_key(text: str) -> str:
    """Return the comparison key for one name spelling."""
    return re.sub(r"\s+", " ", fold(_strip_accents(text))).strip().casefold()


def _is_persian(text: str) -> bool:
    return any("؀" <= character <= "ۿ" for character in str(text))


# ---------------------------------------------------------------- the index


def _load_players() -> list[Player]:
    import database as db

    rows = db.query(
        """
        SELECT p.id, p.first_name, p.second_name, p.web_name,
               p.first_name_fa, p.second_name_fa, p.web_name_fa, p.alias,
               t.short_name AS team
        FROM players AS p
        LEFT JOIN teams AS t ON t.id = p.team_id
        ORDER BY p.id
        """
    )
    players = []
    for row in rows:
        first = str(row.get("first_name") or "").strip()
        second = str(row.get("second_name") or "").strip()
        first_fa = str(row.get("first_name_fa") or "").strip()
        second_fa = str(row.get("second_name_fa") or "").strip()
        canonical = " ".join(part for part in (first, second) if part)
        canonical_fa = " ".join(part for part in (first_fa, second_fa) if part)
        display_fa = str(row.get("web_name_fa") or "").strip() or second_fa
        if not canonical:
            continue
        players.append(
            Player(
                id=int(row["id"]),
                canonical=canonical,
                display=str(row.get("web_name") or "").strip() or second,
                canonical_fa=canonical_fa,
                display_fa=display_fa,
                aliases=tuple(
                    part.strip()
                    for part in str(row.get("alias") or "").split(",")
                    if part.strip()
                ),
                team=str(row.get("team") or "").strip(),
            )
        )
    return players


def _spellings(player: Player) -> list[tuple[str, str]]:
    """Return this player's (spelling, replacement) pairs, longest first.

    The replacement for a full English name is the full Persian name, so a
    translation keeps the same level of detail the source used.
    """
    persian = player.persian
    if not persian:
        return []
    pairs = [
        (player.canonical, player.canonical_fa or persian),
        (player.display, persian),
        (player.canonical.split(" ")[-1], persian),
    ]
    for alias in player.aliases:
        pairs.append((alias, persian))
    # Persian spellings are listed so they can be *detected*; the replacement
    # is a no-op for the canonical ones and the correction for a variant.
    pairs.extend([
        (player.canonical_fa, player.canonical_fa or persian),
        (persian, persian),
    ])
    unique: dict[str, str] = {}
    for spelling, replacement in pairs:
        spelling = str(spelling or "").strip()
        if len(spelling) >= _MIN_NAME_CHARS and spelling not in unique:
            unique[spelling] = replacement
    return sorted(unique.items(), key=lambda pair: len(pair[0]), reverse=True)


def _persian_fragment(spelling: str) -> str:
    """Build a pattern for one Persian spelling that tolerates the variants.

    The stored spelling is folded, so the pattern has to accept every unfolded
    form of each letter, plus a zero-width non-joiner anywhere between them.
    """
    parts = []
    for character in fold(spelling):
        if character.isspace():
            parts.append(rf"[\s{_ZWNJ}]+")
            continue
        group = _EQUIVALENT_LETTERS.get(character)
        parts.append(f"[{group}]" if group else re.escape(character))
    return f"{_ZWNJ}?".join(parts)


def _latin_fragment(spelling: str) -> str:
    """Build a pattern for one Latin spelling, tolerant of how it is spaced.

    ``re.escape`` escapes a space (it is significant under ``re.VERBOSE``), so
    both forms are rewritten to match a line break between the two halves of a
    name as readily as a single space.
    """
    return re.escape(spelling).replace("\\ ", r"\s+").replace(" ", r"\s+")


def _build_index(players: list[Player]) -> _Index:
    replacements: dict[str, tuple[str, bool]] = {}
    owners: dict[str, Player] = {}
    fragments: dict[str, str] = {}
    conflicting: set[str] = set()
    claimed_persian: set[str] = set()

    for player in players:
        for spelling in (player.canonical_fa, player.persian):
            if spelling:
                claimed_persian.add(lookup_key(spelling))

    for player in players:
        for spelling, replacement in _spellings(player):
            key = lookup_key(spelling)
            if not key or key in conflicting:
                continue
            persian_spelling = _is_persian(spelling)
            # A recorded Persian variant must never rewrite a name another
            # player already owns; that would swap one player for another.
            if (
                persian_spelling
                and lookup_key(replacement) != key
                and key in claimed_persian
            ):
                continue
            if key in replacements and replacements[key][0] != replacement:
                # A surname two players share cannot be resolved from the text,
                # so it is dropped rather than guessed.
                conflicting.add(key)
                replacements.pop(key, None)
                owners.pop(key, None)
                fragments.pop(key, None)
                continue
            replacements[key] = (
                replacement,
                not persian_spelling and spelling[:1].isupper(),
            )
            if key in owners and owners[key].id != player.id:
                owners.pop(key)
            else:
                owners[key] = player
            fragments[key] = (
                _persian_fragment(spelling)
                if persian_spelling
                else _latin_fragment(spelling)
            )

    if not fragments:
        return _EMPTY_INDEX

    ordered = sorted(fragments, key=lambda key: len(key), reverse=True)
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(fragments[key] for key in ordered) + r")(?!\w)",
        flags=re.IGNORECASE,
    )
    return _Index(
        players=tuple(players),
        pattern=pattern,
        replacements=replacements,
        owners=owners,
        claimed_persian=frozenset(claimed_persian),
    )


def index() -> _Index:
    """Return the cached name index, rebuilding it when stale."""
    global _index, _index_loaded_at

    with _lock:
        fresh = (
            _index is not None
            and (time.monotonic() - _index_loaded_at) < _CACHE_SECONDS
        )
        if fresh:
            return _index
        try:
            built = _build_index(_load_players())
        except Exception:
            # Name consistency is an improvement on the translation, never a
            # precondition for publishing it.
            logger.exception("Could not load the player name glossary")
            built = _index or _EMPTY_INDEX
        _index = built
        _index_loaded_at = time.monotonic()
        return built


def invalidate() -> None:
    """Drop the cached index after the stored names change."""
    global _index, _index_loaded_at

    with _lock:
        _index = None
        _index_loaded_at = 0.0


def reload() -> None:
    """Rebuild the index now.

    Callers on the event loop should run this in a thread: building it compiles
    a pattern over every stored spelling, and doing that lazily inside a
    translation would stall everything else the bot is doing.
    """
    invalidate()
    index()


def glossary() -> list[dict[str, str]]:
    """Return the full glossary in the shape the transcript corrector wants."""
    return [
        {
            "canonical": player.canonical,
            "display": player.display,
            "canonical_fa": player.canonical_fa,
            "display_fa": player.display_fa,
            "aliases": ", ".join(player.aliases),
            "team": player.team,
        }
        for player in index().players
    ]


# ------------------------------------------------------------ visible text


def _visible_segments(text: str) -> list[str]:
    """Split HTML so replacements never touch a tag, attribute, or URL."""
    return _HTML_SPLIT_RE.split(str(text or ""))


def _map_visible(text: str, transform) -> str:
    segments = _visible_segments(text)
    for position in range(0, len(segments), 2):
        segments[position] = transform(segments[position])
    return "".join(segments)


def visible_text(text: str) -> str:
    return " ".join(_visible_segments(text)[0::2])


# -------------------------------------------------------------- detection


def mentioned(text: str) -> list[Player]:
    """Return the players named in a piece of text, in first-seen order."""
    current = index()
    if current.pattern is None or not text:
        return []

    found: dict[int, Player] = {}
    for segment in _visible_segments(str(text))[0::2]:
        for match in current.pattern.finditer(segment):
            matched = match.group(0)
            key = lookup_key(matched)
            entry = current.replacements.get(key)
            if entry is None or (entry[1] and matched[:1].islower()):
                continue
            player = current.owners.get(key)
            if player is not None:
                found.setdefault(player.id, player)
    return list(found.values())


def prompt_glossary(text: str, limit: int = _PROMPT_GLOSSARY_LIMIT) -> str:
    """Return the prompt block naming the players this text talks about.

    Empty when the text names none, so a prompt never carries a heading with
    nothing under it.
    """
    lines = []
    for player in mentioned(text)[:limit]:
        persian = player.canonical_fa or player.persian
        if not persian:
            continue
        english = player.canonical
        short = player.display
        if short and short.casefold() not in english.casefold():
            english = f"{english} ({short})"
        lines.append(f"- {english} = {persian}")
    if not lines:
        return ""
    return "\n".join([_PROMPT_HEADER, *lines])


# ------------------------------------------------------------ enforcement


def _replace_known(text: str) -> str:
    current = index()
    if current.pattern is None:
        return text

    def substitute(match: re.Match) -> str:
        matched = match.group(0)
        entry = current.replacements.get(lookup_key(matched))
        if entry is None:
            return matched
        replacement, needs_capital = entry
        # A lowercase "white" or "long" is the English word, not the player.
        if needs_capital and matched[:1].islower():
            return matched
        return replacement

    return _map_visible(text, lambda segment: current.pattern.sub(substitute, segment))


def _variant_distance(left: str, right: str, budget: int) -> int:
    """Edit distance in which only a plausible mis-transliteration is cheap.

    Inserting or dropping a letter costs 1, as does swapping one letter for a
    letter transliteration confuses it with. Any other substitution costs 2, so
    an unrelated word cannot slip under a budget of 1.
    """
    if abs(len(left) - len(right)) > budget:
        return budget + 1
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        left_group = _CONFUSABLE.get(left_char)
        for column, right_char in enumerate(right, start=1):
            if left_char == right_char:
                cost = 0
            elif left_group and left_group == _CONFUSABLE.get(right_char):
                cost = 1
            else:
                cost = 2
            current.append(min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + cost,
            ))
        if min(current) > budget:
            return budget + 1
        previous = current
    return previous[-1]


def _contains_word(folded_text: str, spelling: str) -> bool:
    """Whether an already-folded text contains this spelling as a whole word."""
    folded = fold(spelling).strip()
    if not folded:
        return False
    return re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", folded_text) is not None


def _learn_variants(text: str, players: list[Player]) -> str:
    """Correct and record spellings the model invented for known players.

    Only players the source text actually named are considered, and only when
    the translation contains no spelling of that name we already know. That
    makes the question narrow enough to answer by similarity alone: given that
    this article is about Tzolis and no known spelling of زولیس is present,
    a lone Persian word one letter away from it is that name.
    """
    current = index()
    body = visible_text(text)
    if not body:
        return text

    words = _PERSIAN_WORD_RE.findall(body)
    if not words:
        return text
    folded_words = {word: fold(word) for word in set(words)}
    folded_body = fold(body)
    claimed: set[str] = set()

    for player in players:
        persian = player.persian
        if not persian:
            continue
        target = fold(persian)
        # Whole-word, and over the text rather than over single words: a stored
        # name can be two words ("بن دیویس"), while a plain substring test
        # would find زولیس inside the very misspelling being looked for.
        if any(
            _contains_word(folded_body, spelling)
            for spelling in (persian, player.canonical_fa, *player.aliases)
        ):
            continue

        budget = 1 if len(target) <= 6 else 2
        candidates = {
            word
            for word, folded_word in folded_words.items()
            if word not in claimed
            and lookup_key(word) not in current.claimed_persian
            and lookup_key(word) not in current.replacements
            and _variant_distance(folded_word, target, budget) <= budget
        }
        if len(candidates) != 1:
            # Nothing close, or two equally close words: leave the text alone
            # rather than guess which one is the name.
            continue

        variant = candidates.pop()
        # One misspelling cannot be two players at once.
        claimed.add(variant)
        text = _map_visible(
            text,
            lambda segment, variant=variant, persian=persian: re.sub(
                rf"(?<!\w){re.escape(variant)}(?!\w)", persian, segment
            ),
        )
        _record_variant(player, variant)
    return text


def _record_variant(player: Player, variant: str) -> None:
    """Store a newly seen spelling so it is corrected without guessing again."""
    import database as db

    # Store the folded spelling: it is the same name written with the standard
    # Persian letters, so the stored list stays comparable and readable.
    variant = fold(variant)
    try:
        added = db.add_player_alias(player.id, variant, limit=_MAX_LEARNED_VARIANTS)
    except Exception:
        logger.exception(
            "Could not record the spelling %r for player %s", variant, player.canonical
        )
        return
    if added:
        logger.info(
            "Learned Persian spelling %r for %s; corrected to %r",
            variant,
            player.canonical,
            player.persian,
        )
        invalidate()


def enforce(text: str, *, source_text: str = "", learn: bool = True) -> str:
    """Make the stored Persian spelling of every known player win.

    ``source_text`` is the English original. It is what makes the third pass
    safe, so a caller that has it should pass it; without one only spellings
    already known are corrected.
    """
    if not str(text or "").strip():
        return text
    try:
        result = _replace_known(text)
        if learn and source_text:
            players = mentioned(source_text)
            if players:
                result = _learn_variants(result, players)
        return result
    except Exception:
        # Never let name handling cost us a translation.
        logger.exception("Player name enforcement failed")
        return text
