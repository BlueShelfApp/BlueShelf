"""Best-effort metadata extraction from scene file and folder names.

Release names follow no standard, so everything here is heuristic: the parser
fills in what it is reasonably sure of and leaves the rest blank. Performers it
misses are often recovered later by matching names learned from other files
(see text_ngrams / similar_names).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".webm", ".flv",
              ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv"}

LAYOUT_MONO, LAYOUT_SBS, LAYOUT_TB = 0, 1, 2
PROJ_EQUI180, PROJ_EQUI360, PROJ_FISHEYE = 0, 1, 2


@dataclass
class VrInfo:
    is_vr: bool = False
    layout: int = LAYOUT_SBS
    proj: int = PROJ_EQUI180
    lens_fov: float = 190.0


@dataclass
class Parsed:
    title: str = ""
    studio: str = ""
    date: str = ""
    performers: list[str] = field(default_factory=list)
    vr: VrInfo = field(default_factory=VrInfo)
    adult: bool = True
    tags: list[str] = field(default_factory=list)


# Words that never appear in a performer name but are common in scene titles.
TITLE_WORDS = set("""
a an and the of in on at to for with from by into onto over under up down out off
my your his her our their me you him we us it its this that these those what when where who how why
i im is are be was were been am not no yes all any some more most very too so just only again
new old first last best big little small huge giant tiny thick hot sexy wet hard deep rough good bad
great sweet dirty naughty horny kinky wild real true free full
fuck fucks fucked fucking sex sexual anal ass asses booty tits tit boobs titties cock cocks dick dicks pussy
cum cums cumming creampie facial blowjob pov milf milfs teen teens step stepmom stepdad stepsis
stepsister stepbro stepbrother stepdaughter stepson mom mommy dad daddy sister brother daughter son
wife husband girlfriend boyfriend friend friends roommate roomie neighbor boss teacher student nurse
doctor maid massage party pool day night part parts scene vol volume episode ep hd xxx vr video videos
movie clip compilation remastered edition cut official trailer bonus gets get got takes take gives give
wants want likes needs share sharing caught inside outside home house room bed office school job
interview lesson break delivery shipping handling casting audition session show private debut lesbian
""".split())

FOLDER_WORDS = set("""
new folder videos video movies movie porn clips clip downloads download complete completed organized
organised sorted unsorted misc other others stuff temp tmp done watched keep favorites favourites
collection collections scenes scene vr flat best archive old backup incoming torrents transmission
media library
""".split())

# Tokens that mark the end of the meaningful part of a dotted release name.
STRONG_JUNK = set("""
xxx mp4 mkv wmv avi mov webdl webrip hdrip bluray hevc avc h264 h265 x264 x265 1080hd uhd fhd
vr vr180 vr360 3dh 3dv sbs lr 180 360 180x180 mkx200 mkx220 rf52 vrca220 60fps 30fps oculus gearvr
psvr remastered
""".split())
WEAK_JUNK = STRONG_JUNK | set("""
hd sd hq full hi mono tb ou rl go web dl ktr wrb p2p original samsung smartphone desktop quest quest2
quest3 rift mbps kbps fps h 264 265 upscaled
""".split())
_RES_TOKEN = re.compile(r"^(?:\d{3,4}p|\d{3,5}x\d{3,5}|\d{1,2}k|\d{3,5}k|\d{1,3}m|\d{1,3}mbps|fisheye\d*"
                        r"|1080|1440|1920|2048|2160|2880|2900|3072|3600|3840|4000|4096|5400|5760|6000|7200|7680|8192)$")
_CUT_TOKEN = re.compile(r"^(?:\d{3,4}p|[1-9]k|bluray|blu-ray|web-?dl|webrip|hdtv|hdrip|x26[45]|h\.?26[45]|hevc|xxx)$")
_DEVICE_WORDS = {"oculus", "go", "gearvr", "gear", "psvr", "quest", "rift", "smartphone", "desktop", "vive", "h",
                 "pc", "mobile", "samsung", "windows", "mr"}


def is_junk_token(tok: str, strong: bool = False) -> bool:
    t = tok.lower().strip("[](){}!,")
    if not t:
        return True
    if t in (STRONG_JUNK if strong else WEAK_JUNK) or _RES_TOKEN.match(t):
        return True
    return t.isdigit() and (len(t) >= 5 or (len(t) == 4 and t[:2] in ("19", "20")))


# ---------------------------------------------------------------- dates

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_RES = [
    ("mdy_name", re.compile(_MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{2})", re.I)),
    ("dmy_name", re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r",?\s+((?:19|20)\d{2})", re.I)),
    ("ymd", re.compile(r"(?<!\d)((?:19|20)\d{2})[-._ ](\d{1,2})[-._ ](\d{1,2})(?!\d)")),
    ("dmy", re.compile(r"(?<!\d)(\d{1,2})[-._](\d{1,2})[-._]((?:19|20)\d{2})(?!\d)")),
    ("mdy_short", re.compile(r"\[(\d{1,2})-(\d{1,2})-(\d{2})\]")),
]


def _mkdate(y: int, m: int, d: int) -> str:
    if 1970 <= y <= 2099 and 1 <= m <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{m:02d}-{d:02d}"
    return ""


def find_date(text: str) -> tuple[str, tuple[int, int] | None]:
    for kind, rx in _DATE_RES:
        for m in rx.finditer(text):
            g = m.groups()
            if kind == "mdy_name":
                iso = _mkdate(int(g[2]), _MONTHS[g[0].lower()[:3]], int(g[1]))
            elif kind == "dmy_name":
                iso = _mkdate(int(g[2]), _MONTHS[g[1].lower()[:3]], int(g[0]))
            elif kind == "ymd":
                iso = _mkdate(int(g[0]), int(g[1]), int(g[2]))
            elif kind == "dmy":
                iso = _mkdate(int(g[2]), int(g[1]), int(g[0]))
            else:
                iso = _mkdate(2000 + int(g[2]), int(g[0]), int(g[1]))
            if iso:
                return iso, m.span()
    return "", None


# ---------------------------------------------------------------- names

_NAME_WORD = re.compile(r"^[^\W\d_][^\W\d_'.-]*(?:['.-][^\W\d_]*)*\.?$")
_LIST_SPLIT = re.compile(r"\s*(?:,|&|\+|\band\b)\s*", re.I)
_LIST_SPLIT_WITH = re.compile(r"\s*(?:,|&|\+|\band\b|\bwith\b)\s*", re.I)
_CAMEL_NAME = re.compile(r"^([A-Z][a-z]{2,})([A-Z][a-z]{1,})$")


def performer_key(name: str) -> str:
    """Identity key for a performer: case, accents, punctuation and doubled letters ignored."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"'s\b", "", s)
    s = re.sub(r"[^a-z ]+", " ", s)
    s = re.sub(r"([a-z])\1+", r"\1", s)
    return " ".join(s.split())


def similar_names(a: str, b: str) -> bool:
    """True when two performer names are probably spellings of the same person."""
    ka, kb = performer_key(a), performer_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    wa, wb = ka.split(), kb.split()
    if wa[0] == wb[0]:
        if len(wa) == 1 or len(wb) == 1:
            return True
        la, lb = wa[-1], wb[-1]
        if (len(la) == 1 and lb.startswith(la)) or (len(lb) == 1 and la.startswith(lb)):
            return True
        return SequenceMatcher(None, la, lb).ratio() >= 0.6
    return SequenceMatcher(None, ka, kb).ratio() >= 0.88


def smart_title(text: str) -> str:
    words = []
    for w in text.split():
        if w.islower() or (w.isupper() and len(w) > 2):
            w = w[:1].upper() + w[1:].lower()
        words.append(w)
    return " ".join(words)


def _name_word_ok(word: str) -> bool:
    return bool(_NAME_WORD.match(word)) and word.lower().strip(".") not in TITLE_WORDS \
        and word.lower() not in WEAK_JUNK


def looks_like_name(text: str, allow_single: bool = False) -> bool:
    words = text.split()
    if not words or len(words) > 3 or (len(words) == 1 and not allow_single):
        return False
    if len(words) == 1 and len(words[0]) < 3:
        return False
    return all(_name_word_ok(w) for w in words)


def split_names(text: str, allow_single: bool = False) -> list[str] | None:
    """Split 'A, B & C' into names, or None when any part does not look like a name."""
    splitter = _LIST_SPLIT_WITH if re.search(r"[,&+]", text) else _LIST_SPLIT
    parts = [x.strip(" .-") for x in splitter.split(text) if x.strip(" .-")]
    if not parts:
        return None
    multi = len(parts) > 1
    if all(looks_like_name(x, allow_single or multi) for x in parts):
        return [smart_title(x) for x in parts]
    return None


def is_person_folder(name: str) -> bool:
    name = name.strip()
    if not re.fullmatch(r"[^\W\d_][^\W\d_'.-]*(?: [^\W\d_][^\W\d_'.-]*){1,2}", name):
        return False
    return not any(w in TITLE_WORDS or w in FOLDER_WORDS or w in WEAK_JUNK for w in name.lower().split())


def text_ngrams(text: str) -> set[str]:
    """2- and 3-word performer keys found anywhere in text, for matching known names."""
    grams: set[str] = set()
    for variant in (text, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)):
        words = performer_key(re.sub(r"[_\W]+", " ", re.sub(r"'s\b", "", variant))).split()
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                grams.add(" ".join(words[i:i + n]))
    return grams


# ---------------------------------------------------------------- building blocks

_PAREN = re.compile(r"[(\[{]([^)\]}]*)[)\]}]")
_STARRING = re.compile(r"\b(?:starring|featuring|feat\.?|ft\.)\s+([^_()\[\]]+)", re.I)
_BRACKET_STUDIO = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
_SCENE_YMD = re.compile(r"^(?P<studio>[A-Za-z][A-Za-z0-9]*(?: [A-Z][A-Za-z0-9]*)?)[ ._-]"
                        r"(?P<y>\d{2}|(?:19|20)\d{2})[ ._-](?P<m>\d{2})[ ._-](?P<d>\d{2})[ ._-]+(?P<rest>.+)$")
_SCENE_DMY = re.compile(r"^(?P<studio>[A-Za-z][A-Za-z0-9]*)[ ._-](?P<d>\d{2})[ ._-](?P<m>\d{2})[ ._-]"
                        r"(?P<y>(?:19|20)\d{2})[ ._-]+(?P<rest>.+)$")
_CODE = re.compile(r"^(?P<studio>[A-Za-z]{2,6}\d?)-(?P<num>\d{2,5})(?:-(?P<part>\d{1,2}))?$")
_COMPACT_DATE = re.compile(r"^(?P<studio>[A-Za-z]{2,6})-(?P<ymd>\d{6})-(?P<rest>.+)$")
_PART_STEM = re.compile(r"\d{1,3}|(?:part|pt|cd|disc|scene)[ ._-]?\d{1,2}", re.I)


def _is_tech_text(text: str) -> bool:
    toks = [t for t in re.split(r"[\s,._+-]+", text) if t]
    return bool(toks) and all(is_junk_token(t) or t.lower() in _DEVICE_WORDS for t in toks)


def _has_device_parens(text: str) -> bool:
    """'(Oculus 8K)' style groups, which VR releases put right after the performer names."""
    for m in _PAREN.finditer(text):
        words = {t.lower() for t in re.split(r"[\s,._+-]+", m.group(1)) if t}
        if _is_tech_text(m.group(1)) and words & (_DEVICE_WORDS | {"vr", "vr180", "3d"}):
            return True
    return False


def _strip_tech(text: str) -> str:
    """Remove technical groups like '(Oculus 8K)' and everything from a resolution/codec token on."""
    if " " in text:
        text = text.replace("_", " ")
    text = re.sub(r"[-_](?=(?:\d{3,4}p|\d{1,2}k)\b)", " ", text, flags=re.I)
    text = _PAREN.sub(lambda m: " " if not m.group(1).strip() or _is_tech_text(m.group(1)) else m.group(0), text)
    toks = text.split()
    for i, t in enumerate(toks):
        if i > 0 and _CUT_TOKEN.match(t.lower().strip("[](){},")):
            toks = toks[:i]
            break
    while toks and (is_junk_token(toks[-1]) or toks[-1].lower().strip("()[]") in _DEVICE_WORDS
                    or not re.search(r"\w", toks[-1])):
        toks.pop()
    return " ".join(toks).strip(" -_.,")


def clean_studio(text: str) -> str:
    text = text.strip(" []")
    if re.search(r"\.(com|net|org|xxx)\b", text, re.I):
        text = text.split()[0]
    text = re.sub(r"\.(com|net|org|xxx)\b", "", text, flags=re.I).strip(" .-_")
    return smart_title(text) if text.islower() else text


def _add_performers(p: Parsed, names: list[str]) -> None:
    keys = {performer_key(n) for n in p.performers}
    for n in names:
        k = performer_key(n)
        if k and k not in keys:
            keys.add(k)
            p.performers.append(n)


def _add_folder_performer(p: Parsed, name: str) -> None:
    """Add a performer taken from a folder name, unless the file already names a spelling of them."""
    for i, existing in enumerate(p.performers):
        if similar_names(existing, name):
            if len(performer_key(existing)) < len(performer_key(name)) and len(existing.split()) < 2:
                p.performers[i] = name
            return
    p.performers.append(name)


def _names_from_tokens(tokens: list[str], default_pair: bool = True) -> tuple[list[str], list[str]]:
    """Split leading performer names off a dotted scene-release token list."""
    lowered = [t.lower() for t in tokens]
    if "and" in lowered[:7]:
        i = lowered.index("and")
        before, after = tokens[:i], tokens[i + 1:]
        if len(before) in (1, 2, 4) and after:
            names = [" ".join(before[j:j + 2]) for j in range(0, len(before), 2)]
            take = 2 if len(after) >= 2 and _name_word_ok(after[1]) else 1
            names.append(" ".join(after[:take]))
            if all(looks_like_name(n, allow_single=True) for n in names):
                return [smart_title(n) for n in names], tokens[i + 1 + take:]
    if not default_pair:
        return [], tokens
    if len(tokens) == 1 and looks_like_name(tokens[0], allow_single=True):
        return [smart_title(tokens[0])], []
    if len(tokens) >= 2 and looks_like_name(" ".join(tokens[:2])):
        return [smart_title(" ".join(tokens[:2]))], tokens[2:]
    return [], tokens


def _tokens_until_junk(tokens: list[str]) -> list[str]:
    out = []
    for t in tokens:
        if is_junk_token(t, strong=True):
            if out:
                break
            continue
        out.append(t)
    while out and is_junk_token(out[-1]):
        out.pop()
    return out


def _extract_paren_names(text: str, p: Parsed) -> str:
    def replace(match: re.Match) -> str:
        inner = match.group(1).strip()
        if " " not in inner:
            inner = inner.replace(".", " ")
        names = split_names(inner, allow_single=" " in inner or "," in inner)
        if names and not _is_tech_text(inner):
            _add_performers(p, names)
            return " "
        return match.group(0)

    return " ".join(_PAREN.sub(replace, text).split())


def _parse_segments(segs: list[str], p: Parsed, studio_first: bool, prefer_last: bool = False,
                    hints: tuple[str, ...] = ()) -> None:
    """'Studio - Title - Performers (Device)' and its many variations."""
    segs = [s.strip(" ._-") for s in segs]
    segs = [s for s in segs if s]
    if studio_first and len(segs) >= 2 and not p.studio and re.match(r"[A-Za-z]", segs[0]):
        first_names = split_names(segs[0])
        if not (first_names and len(first_names) > 1):
            p.studio = clean_studio(segs.pop(0))
    items: list[list] = []
    for s in segs:
        device = _has_device_parens(s)
        cleaned = _strip_tech(s)
        if cleaned and not _is_tech_text(cleaned) and not re.fullmatch(r"\d{1,4}", cleaned):
            items.append([cleaned, split_names(cleaned), device])
    candidates = [i for i, item in enumerate(items) if item[1]]
    perf_idx = None
    if candidates:
        hint_keys = {performer_key(h) for h in hints}
        exact = [i for i in candidates if any(performer_key(n) in hint_keys for n in items[i][1])]
        hinted = exact or [i for i in candidates if any(similar_names(n, h) for n in items[i][1] for h in hints)]
        with_device = [i for i in candidates if items[i][2]]
        # A tie between name-like segments is ambiguous: VR studios write "Title - Performer", others the reverse.
        position = (lambda i: i) if prefer_last else (lambda i: -i)
        if hinted:
            perf_idx = hinted[0]
        elif with_device:
            perf_idx = with_device[-1]
        else:
            perf_idx = max(candidates, key=lambda i: (len(items[i][1]), position(i)))
    elif len(items) >= 2:
        last = split_names(items[-1][0], allow_single=True)
        if last:
            items[-1][1] = last
            perf_idx = len(items) - 1
    if perf_idx is not None:
        _add_performers(p, items[perf_idx][1])
    title = " - ".join(item[0] for i, item in enumerate(items) if i != perf_idx)
    if title and not p.title:
        p.title = title


def _parse_tokens(tokens: list[str], p: Parsed, allow_studio: bool, force_studio: bool = False) -> None:
    """Dotted, hyphenated or underscored names with no spaces."""
    tokens = [t for t in tokens if t]
    if not tokens:
        return
    first = tokens[0]
    lower_slug = not any(c.isupper() for t in tokens for c in t)
    studio_like = len(tokens) > 1 and (force_studio or bool(re.search(r"[a-z][A-Z]", first))
                                       or first.lower().endswith("vr") or first.lower().startswith("vr"))
    names: list[str] = []
    rest = tokens
    if lower_slug and len(tokens) > 3:
        names, after = _names_from_tokens(tokens[1:], default_pair=False)
        if names:
            rest = after
            studio_like = True
    if allow_studio and studio_like and re.match(r"[A-Za-z]", first):
        p.studio = p.studio or (smart_title(first) if first.islower() else first)
        if not names:
            rest = tokens[1:]
    title_tokens = []
    for t in _tokens_until_junk(rest):
        m = _CAMEL_NAME.match(t)
        if m and not names and t != first:
            _add_performers(p, [f"{m.group(1)} {m.group(2)}"])
        else:
            title_tokens.append(t)
    _add_performers(p, names)
    p.title = p.title or smart_title(" ".join(title_tokens))


def _parse_underscored(stem: str, p: Parsed, bracket: bool) -> None:
    segs = [s for s in stem.split("_") if s.strip()]
    spaced = any(" " in s or "+" in s for s in segs)
    slr = False
    if len(segs) > 1 and segs[0].upper() == "SLR":
        slr = True
        segs = segs[1:]
        if len(segs) > 2 and segs[0].upper() == "SLR" and segs[1].lower() == "originals":
            segs = ["SLR Originals"] + segs[2:]
    if not spaced:
        if slr and len(segs) > 1:
            p.studio = p.studio or segs[0]
            segs = segs[1:]
        tokens = [t for s in segs for t in re.split(r"[-.]+", s)]
        _parse_tokens(tokens, p, allow_studio=not bracket and not slr)
        return

    segs = [s if " " in s else s.replace("-", " ") for s in segs]
    if not bracket and len(segs) > 1 and (slr or re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", segs[0])):
        p.studio = p.studio or clean_studio(segs.pop(0))
    parts = []
    for s in segs:
        s = _strip_tech(s)
        if s and not _is_tech_text(s) and not re.fullmatch(r"\d+", s):
            parts.append(s)
    merged: list[str] = []
    i = 0
    while i < len(parts):  # "Keisha_Grey" arrives as two one-word segments
        if i + 1 < len(parts) and " " not in parts[i] and " " not in parts[i + 1] \
                and looks_like_name(f"{parts[i]} {parts[i + 1]}"):
            merged.append(f"{parts[i]} {parts[i + 1]}")
            i += 2
        else:
            merged.append(parts[i])
            i += 1
    if len(merged) == 1 and merged[0] in parts and split_names(merged[0]):
        _add_performers(p, split_names(merged[0]))
        return
    taken = 0
    for s in merged[:-1]:
        names = split_names(s)
        if not names:
            break
        _add_performers(p, names)
        taken += 1
    if not p.title:
        p.title = " - ".join(merged[taken:])


def _parse_scene_rest(rest: str, p: Parsed) -> None:
    first_chunk = rest.split(" ", 1)[0]
    if " " not in rest or "." in first_chunk or "_" in first_chunk:
        tokens = _tokens_until_junk(re.split(r"[._\-\s]+", rest))
        names, title_tokens = _names_from_tokens(tokens)
        _add_performers(p, names)
        p.title = smart_title(" ".join(title_tokens))
        return
    rest = _extract_paren_names(rest, p)
    head, sep, tail = rest.partition(" - ")
    names = split_names(head.replace(".", ","), allow_single=True) if sep else None
    if names:
        _add_performers(p, names)
        p.title = _strip_tech(tail)
    elif sep:
        _parse_segments(re.split(r"\s+-\s+", rest), p, studio_first=False)
    else:
        p.title = _strip_tech(rest)


# ---------------------------------------------------------------- entry points

def parse_name(stem: str, hints: tuple[str, ...] = ()) -> Parsed:
    """Parse one file or folder name. hints are performer names known from the folder structure."""
    p = Parsed()
    stem = " ".join(stem.split())

    if " = " in stem:
        left, right = stem.split(" = ", 1)
        names = split_names(left)
        if names and " " not in right.strip():
            _add_performers(p, names)
            p.studio = clean_studio(right)
            return p

    m = _CODE.match(stem)
    if m:
        p.studio = m.group("studio").upper()
        p.title = f"{p.studio}-{m.group('num')}" + (f" (Part {m.group('part')})" if m.group("part") else "")
        return p
    m = _COMPACT_DATE.match(stem)
    if m:
        ymd = m.group("ymd")
        p.studio = m.group("studio").upper()
        p.date = _mkdate(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
        p.title = smart_title(" ".join(_tokens_until_junk(re.split(r"[-_. ]+", m.group("rest")))))
        return p

    for rx in (_SCENE_YMD, _SCENE_DMY):
        m = rx.match(stem)
        if not m:
            continue
        y = int(m.group("y"))
        date = _mkdate(y if y > 99 else 2000 + y, int(m.group("m")), int(m.group("d")))
        if date:
            p.date = date
            p.studio = clean_studio(m.group("studio"))
            _parse_scene_rest(m.group("rest"), p)
            return p

    date, span = find_date(stem)
    if date:
        p.date = date
        stem = stem[:span[0]] + " " + stem[span[1]:]
        stem = " ".join(re.sub(r"[(\[{]\s*[)\]}]", " ", stem).split())

    m = _STARRING.search(stem)
    if m:
        names = split_names(_strip_tech(m.group(1)), allow_single=True)
        if names:
            _add_performers(p, names)
            stem = (stem[:m.start()] + " " + stem[m.end():]).strip()

    m = _BRACKET_STUDIO.match(stem)
    bracket = bool(m)
    if m:
        p.studio = clean_studio(m.group(1))
        stem = m.group(2)

    stem = _extract_paren_names(stem, p)

    if not bracket and re.match(r"^[A-Za-z0-9]+_", stem):
        _parse_underscored(stem, p, bracket)
    elif re.search(r"\s+-\s+", stem):
        vr_style = detect_vr(stem).is_vr or p.studio.lower().endswith("vr")
        _parse_segments(re.split(r"\s+-\s+", stem), p, studio_first=not bracket, prefer_last=vr_style, hints=hints)
    elif bracket and "-" in stem:
        head, _, tail = stem.partition("-")
        names = split_names(head)
        if names:
            _add_performers(p, names)
            p.title = _strip_tech(tail)
        else:
            p.title = _strip_tech(stem)
    elif "_" in stem:
        _parse_underscored(stem, p, bracket)
    elif " " not in stem and re.search(r"[.\-]", stem):
        _parse_tokens(re.split(r"[.\-]+", stem), p, allow_studio=not bracket)
    elif "," in stem:
        parts = re.split(r"\s*,\s*", stem)
        m = re.match(r"^([A-Za-z][A-Za-z0-9]*)-(\S.*)$", parts[0])
        if m and not p.studio:
            p.studio = m.group(1)
            parts[0] = m.group(2)
        titles = []
        for s in parts:
            s = _strip_tech(s)
            if not s or _is_tech_text(s):
                continue
            names = split_names(s)
            if names:
                _add_performers(p, names)
            else:
                titles.append(s)
        p.title = ", ".join(titles)
    else:
        p.title = _strip_tech(stem)

    if not p.title and not p.performers:
        p.title = clean_title(stem)
    return p


def clean_title(text: str) -> str:
    if " " not in text.strip():
        return smart_title(" ".join(_tokens_until_junk(re.split(r"[._\-]+", text)))) or text
    return _strip_tech(text) or text


# ---------------------------------------------------------------- VR / adult / tags

_VR_TOKENS = {"vr", "vr180", "vr360", "3dh", "3dv", "sbs", "oculus", "gearvr", "psvr", "quest", "quest2",
              "quest3", "180x180", "sexlikereal", "slr", "vrdesktophd"}
_FISHEYE_LENSES = {"mkx200": 200.0, "mkx220": 220.0, "rf52": 190.0, "vrca220": 220.0}


def detect_vr(text: str) -> VrInfo:
    tokens = set(re.split(r"[^a-z0-9]+", text.lower())) - {""}
    has_180 = bool(tokens & {"180", "180x180", "vr180"})
    has_360 = bool(tokens & {"360", "vr360", "mono360"})
    fisheye = next((t for t in tokens if t.startswith("fisheye") or t in _FISHEYE_LENSES), None)
    studio_vr = any(len(t) > 3 and t.isalpha() and (t.endswith("vr") or t.startswith("vr")) for t in tokens)
    layout_hint = bool(tokens & {"lr", "rl", "tb", "ou", "sbs", "3d", "mono"})
    info = VrInfo()
    info.is_vr = bool(tokens & _VR_TOKENS) or fisheye is not None or studio_vr or \
        ((has_180 or has_360) and layout_hint)
    if tokens & {"tb", "ou", "3dv", "overunder", "topbottom"}:
        info.layout = LAYOUT_TB
    elif tokens & {"mono", "mono360", "2d"}:
        info.layout = LAYOUT_MONO
    if fisheye:
        info.proj = PROJ_FISHEYE
        m = re.match(r"fisheye(\d{3})", fisheye)
        info.lens_fov = float(m.group(1)) if m else _FISHEYE_LENSES.get(fisheye, 190.0)
    elif has_360 and not has_180:
        info.proj = PROJ_EQUI360
    return info


def normalize_for_match(text: str) -> str:
    return " ".join(re.sub(r"[_\W]+", " ", text.lower()).split())


_SOURCE_TAG = r"(?:bluray|blu ray|web ?dl|webrip|hdtv|bdrip|brrip|dvdrip|telesync|hdcam|amzn|hmax|dsnp|atvp|remux|nf)"
_NON_ADULT = re.compile(
    r"\bs\d{1,2}e\d{1,3}\b|\bseason \d"
    r"|(?:\b(?:19|20)\d{2}\b(?! \d{1,2} \d{1,2}\b)|\bs\d{2}\b).*\b" + _SOURCE_TAG + r"\b")
_ADULT = re.compile(r"\bxxx|porn|brazzers|blacked|bangbros|naughty ?america|reality ?kings|tushy|vixen"
                    r"|onlyfans|manyvids|sexlikereal|\bslr\b|\bnsfw\b|hentai")

DEFAULT_TAG_RULES = """\
Anal = \\banal\\b
Creampie = cream ?pie
MILF = \\bmilfs?\\b|\\bcougars?\\b|\\bmommy\\b
Step Fantasy = \\bstep ?(mom|dad|sis|sister|bro|brother|daughter|son|family|mother|father)|taboo|sisswap|family ?therapy
Threesome = threesome|\\b(mff|ffm|mmf|fmm)\\b
Group = gang ?bang|orgy|bukkake|foursome
Lesbian = lesbian|girl ?on ?girl
Massage = massage|\\bnuru
Cosplay / Parody = parody|cosplay
POV = \\bpov\\b
Trans = \\btrans\\b|transfixed|\\bts\\b|tspussy
Oral = blow ?job|deep ?throat|glory ?hole|swallow|\\bfacial
Big Tits = big ?(tits|boobs|breasts?)|busty|titties
Big Ass = \\bpawg\\b|big ?(ass|butt|booty)|thicc
Interracial = \\bbbc\\b|blacked|interracial
Amateur = amateur|casting|homemade
JAV = \\bjav\\b|heyzo|kin8|1pondo
Latina = latina|sexmex|colombian
Solo = masturbat|\\bsolo\\b
Passthrough = passthrough|\\balpha\\b|chroma ?key|green ?screen
"""


def parse_tag_rules(text: str) -> list[tuple[str, re.Pattern]]:
    rules = []
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, _, pattern = line.partition("=")
        try:
            rules.append((name.strip(), re.compile(pattern.strip(), re.I)))
        except re.error:
            continue
    return rules


def bad_tag_rule_lines(text: str) -> list[str]:
    bad = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, pattern = line.partition("=")
        if not sep or not name.strip() or not pattern.strip():
            bad.append(f"line {n}: {line.strip()}")
            continue
        try:
            re.compile(pattern.strip())
        except re.error as exc:
            bad.append(f"line {n}: {exc}")
    return bad


def auto_tags(norm_text: str, rules: list[tuple[str, re.Pattern]]) -> list[str]:
    return [name for name, rx in rules if name and rx.search(norm_text)]


# ---------------------------------------------------------------- whole paths

def _richness(p: Parsed) -> float:
    return 3 * len(p.performers) + 2 * bool(p.date) + bool(p.studio) + min(len(p.title.split()), 4) / 4


def parse_path(rel_path: str, single_video_dir: bool = True,
               tag_rules: list[tuple[str, re.Pattern]] | None = None) -> Parsed:
    parts = [x for x in re.split(r"[\\/]+", rel_path) if x]
    filename = parts[-1]
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    dirs = parts[:-1]

    person_folders = tuple(smart_title(f) for f in dirs if is_person_folder(f))
    p = parse_name(stem, person_folders)
    numbered = bool(_PART_STEM.fullmatch(stem))
    if dirs and (single_video_dir or numbered) and not is_person_folder(dirs[-1]):
        d = parse_name(dirs[-1], person_folders)
        primary, secondary = (d, p) if numbered or _richness(d) > _richness(p) else (p, d)
        merged = Parsed(title=primary.title or secondary.title, studio=primary.studio or secondary.studio,
                        date=primary.date or secondary.date)
        _add_performers(merged, primary.performers + secondary.performers)
        if numbered:
            merged.title = f"{d.title or dirs[-1]} ({stem})"
        p = merged
    in_person_folder = bool(person_folders)
    for name in person_folders:
        _add_folder_performer(p, name)
    if not p.title:
        p.title = ", ".join(p.performers) if p.performers else clean_title(stem)

    norm = normalize_for_match(rel_path)
    p.vr = detect_vr(rel_path)
    p.adult = (bool(_ADULT.search(norm)) or p.vr.is_vr or bool(p.date) or in_person_folder
               or not _NON_ADULT.search(norm))
    p.tags = auto_tags(norm, tag_rules if tag_rules is not None else parse_tag_rules(DEFAULT_TAG_RULES))
    return p
