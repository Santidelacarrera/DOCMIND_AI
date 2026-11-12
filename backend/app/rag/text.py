"""Tokenisation and sentence helpers for claim verification (ported from ContextRAG_Engine ``core/text.py``)."""

import re

_TOKEN = re.compile(r"\w+", re.UNICODE)

STOPWORDS = frozenset(
    ["a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these", "those", "with", "as", "by", "at", "from", "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "y", "o", "en", "es", "son", "fue", "ser", "se", "su", "sus", "que", "con", "por", "para", "como", "lo"]
)


ABBREVIATIONS = frozenset(["dr", "dra", "sr", "sra", "srta", "mr", "mrs", "ms", "prof", "profa", "ing", "lic", "ud", "uds", "vs", "etc", "approx", "aprox", "inc", "ltd", "corp", "co", "fig", "figs", "sec", "art", "arts", "núm", "num", "pág", "pp", "cap", "caps", "ej", "eg", "ie", "cf", "al", "jr", "st", "e.g", "i.e", "u.s", "ee.uu", "a.m", "p.m", "p", "vol"])


def is_abbreviation(word: str) -> bool:
    """True for tokens whose trailing period does not end a sentence ("Dr.", "e.g.", "etc.", "Inc.")."""
    w = word.lower().rstrip(".")
    return w in ABBREVIATIONS


# Company-form suffixes ("S.A.", "Ltda.", "SpA"). They are abbreviations *and* can end a sentence, so unlike "Dr." they
# only keep the sentence going when what follows starts in lower case or with a digit ("...Valdivia S.A. es de 898...").
CORPORATE_SUFFIXES = frozenset(["s.a", "s.a.s", "s.p.a", "spa", "ltda", "ltd", "cia", "cía", "hnos", "s.l", "s.r.l", "eirl", "e.i.r.l"])


_OPENERS = "[\"'(¿¡«“"
_BREAK_OPENERS = "[\"'(¿¡"  # exactly the optional opener of the original sentence-end pattern


def _word_char(c: str) -> bool:
    return c.isalnum() or c == "_" or "À" <= c <= "ÿ"


def sentence_breaks(text: str, *, word_after: bool = True) -> list[tuple[int, int]]:
    """Spans of sentence breaks: whitespace after ``.!?`` (followed, when ``word_after``, by an optional
    opening quote or bracket and a word character), or a run of newlines.

    Written as a single forward scan. The regular expression it replaces (punctuation look-behind,
    whitespace run, look-ahead) backtracks over long whitespace runs (CodeQL py/polynomial-redos).
    """
    out: list[tuple[int, int]] = []
    n, i = len(text), 0
    while i < n:
        c = text[i]
        if c.isspace() and i > 0 and text[i - 1] in ".!?":
            j = i
            while j < n and text[j].isspace():
                j += 1
            k = j + 1 if j < n and text[j] in _BREAK_OPENERS else j
            if not word_after or (k < n and _word_char(text[k])):
                out.append((i, j))
                i = j
                continue
        if c == "\n":
            j = i
            while j < n and text[j] == "\n":
                j += 1
            out.append((i, j))
            i = j
            continue
        i += 1
    return out


def split_at(text: str, breaks: list[tuple[int, int]]) -> list[str]:
    parts, position = [], 0
    for start, end in breaks:
        parts.append(text[position:start])
        position = end
    parts.append(text[position:])
    return parts


def _word_before_final_period(segment: str) -> str | None:
    """The run of word characters and dots before a final full stop ("... Dr." -> "Dr"), scanning back."""
    s = segment.rstrip()
    if not s.endswith("."):
        return None
    i = len(s) - 1
    while i > 0 and (s[i - 1].isalnum() or s[i - 1] in "._"):
        i -= 1
    return s[i : len(s) - 1] or None


def _ends_with_corporate_suffix(segment: str) -> bool:
    word = _word_before_final_period(segment)
    return word is not None and word.lower().rstrip(".") in CORPORATE_SUFFIXES


_CORPORATE_CANDIDATE = re.compile(r"\b([A-Za-zÁÉÍÓÚÑÜáéíóúñü][\w.]{0,10}?)\.(?=\s+[\[\"'(¿¡«“]*[a-záéíóúñü0-9])")


def protect_corporate_suffixes(text: str) -> str:
    """For analysis that tokenises punctuation: drop the full stops of a company-form suffix that a sentence continues
    after ("Valdivia S.A. es de ..." becomes "Valdivia SA es de ..."), so they are not read as sentence ends. Internal
    only; text shown to a user is never rewritten."""
    def fix(m: re.Match[str]) -> str:
        return m.group(1).replace(".", "") if m.group(1).lower().rstrip(".") in CORPORATE_SUFFIXES else m.group(0)

    return _CORPORATE_CANDIDATE.sub(fix, text)


def _continues_in_lower_case(text: str, pos: int) -> bool:
    chunk = text[pos : pos + 6]
    j = 0
    while j < len(chunk) and chunk[j] in _OPENERS:
        j += 1
    if j >= len(chunk) or not (chunk[j].isalnum() or chunk[j] == "_"):
        return False
    return chunk[j].islower() or chunk[j].isdigit()


def _ends_with_abbreviation(segment: str) -> bool:
    word = _word_before_final_period(segment)
    return word is not None and is_abbreviation(word)


def ends_with_break(gap: str, marks: str) -> bool:
    """True when ``gap`` (the text between two words) ends a sentence: a newline in its trailing
    whitespace, or one of ``marks`` just before it. Linear string checks, not a trailing-whitespace
    regex, which backtracks quadratically on long runs of whitespace (CodeQL py/polynomial-redos)."""
    body = gap.rstrip()
    if "\n" in gap[len(body):]:
        return True
    return bool(body) and body[-1] in marks


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def content_tokens(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in STOPWORDS]


def stem(token: str) -> str:
    """Very light suffix stripping; numbers are left untouched."""
    if token.isdigit() or len(token) <= 4:
        return token
    for suf in ("ing", "ed", "es", "s", "ción", "ciones", "mente"):
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            return token[: -len(suf)]
    return token


def split_sentences(text: str, base: int = 0) -> list[tuple[str, int, int]]:
    """Split ``text`` into (sentence, start, end) with offsets shifted by ``base``."""
    out: list[tuple[str, int, int]] = []
    pos = 0
    spans: list[tuple[int, int] | None] = [*sentence_breaks(text), None]
    for m in spans:
        newline = m is not None and "\n" in text[m[0] : m[1]]
        if m and not newline and _ends_with_abbreviation(text[pos : m[0]]):
            continue  # "Dr. Smith", "e.g. this": the period does not end the sentence
        if (m and not newline and _ends_with_corporate_suffix(text[pos : m[0]])
                and _continues_in_lower_case(text, m[1])):
            continue  # "Valdivia S.A. es de ...": a company form, and the sentence goes on
        end = m[0] if m else len(text)
        seg = text[pos:end]
        stripped = seg.strip()
        if stripped:
            s = pos + seg.index(stripped)
            out.append((stripped, base + s, base + s + len(stripped)))
        if m:
            pos = m[1]
    return out
