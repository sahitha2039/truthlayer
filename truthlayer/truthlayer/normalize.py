"""Type-driven normalizers. Each returns (value, problem) where problem is None or a
short string explaining why the raw value could not be trusted.

Nothing here knows about healthcare: vocabularies (roles, facilities) come from the
client config and are passed in as `ref` alias maps.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

EMPTY = {"", "na", "n/a", "none", "null", "-", "--", "nan"}


def is_empty(v) -> bool:
    return v is None or str(v).strip().lower() in EMPTY


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def simplify(s: str) -> str:
    """lowercase, strip accents/punctuation, collapse whitespace."""
    s = _ascii(str(s)).lower().replace("&", " and ")
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- dates
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d",
                "%d-%b-%Y", "%d %b %Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y",
                "%Y%m%d", "%m.%d.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]


def norm_date(raw):
    if is_empty(raw):
        return None, None
    s = str(raw).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat(), None
        except ValueError:
            pass
    if re.fullmatch(r"\d{5}(\.0+)?", s):  # Excel serial date
        n = int(float(s))
        if 20000 < n < 80000:
            return (date(1899, 12, 30) + timedelta(days=n)).isoformat(), None
    return None, f"'{s}' is not a valid date"


# ---------------------------------------------------------------- numbers / phones / ids
def norm_number(raw, lo=None, hi=None):
    if is_empty(raw):
        return None, None
    m = re.search(r"-?\d+(?:\.\d+)?", str(raw).replace(",", ""))
    if not m:
        return None, f"'{raw}' is not a number"
    v = float(m.group())
    v = int(v) if v.is_integer() else v
    if lo is not None and v < lo:
        return v, f"{v} is below the allowed minimum ({lo})"
    if hi is not None and v > hi:
        return v, f"{v} is above the expected maximum ({hi})"
    return v, None


def norm_phone(raw):
    if is_empty(raw):
        return None, None
    d = re.sub(r"\D", "", str(raw))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10:
        return d or None, f"'{raw}' is not a 10-digit phone number"
    return f"{d[:3]}-{d[3:6]}-{d[6:]}", None


def norm_id(raw):
    if is_empty(raw):
        return None, None
    return re.sub(r"\s+", "", str(raw)).upper(), None


def norm_license(raw):
    """RN-551203, 'rn 551203', 'RN551203', 'RN#551203' all -> RN-551203."""
    if is_empty(raw):
        return None, None
    s = re.sub(r"[\s#._/\-]+", "", _ascii(str(raw)).upper())
    m = re.fullmatch(r"([A-Z]+)(\d+)", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}", None
    return s, None


# ---------------------------------------------------------------- coded values
def build_ref(alias_map: dict) -> dict:
    """{CODE: [aliases]} -> {simplified alias: CODE}"""
    out = {}
    for code, aliases in alias_map.items():
        out[simplify(code)] = code
        for a in aliases:
            out[simplify(a)] = code
    return out


def norm_code(raw, ref: dict):
    if is_empty(raw):
        return None, None
    s = simplify(raw)
    if s in ref:
        return ref[s], None
    # whole-word containment, longest alias first ("Harborview Bayside SNF", "RN Supervisor")
    for alias in sorted(ref, key=len, reverse=True):
        if len(alias) >= 2 and re.search(rf"\b{re.escape(alias)}\b", s):
            return ref[alias], None
    best, score = None, 0.0
    for alias, code in ref.items():
        r = SequenceMatcher(None, s, alias).ratio()
        if r > score:
            best, score = code, r
    if score >= 0.88:
        return best, None
    return None, f"'{raw}' is not a recognised value"


# ---------------------------------------------------------------- person names
_NICK_GROUPS = [
    "marcus marc mark", "katherine kate katie kathy catherine cathy kat kathryn", "robert rob bob bobby robbie bert",
    "william will bill billy liam", "james jim jimmy jamie", "elizabeth liz beth betty eliza lizzie libby",
    "michael mike mikey mick", "jonathan jon john johnny jonathon", "daniel dan danny", "christopher chris topher",
    "jennifer jen jenny jenn", "patricia pat patty trish tricia", "margaret maggie meg peggy marge",
    "anthony tony", "joseph joe joey", "thomas tom tommy", "richard rick rich dick ricky", "susan sue suzy susie",
    "deborah deb debbie debra", "alexander alex xander", "alexandra alex alexa sandra", "samuel sam sammy",
    "samantha sam sammie", "nicholas nick nicky", "benjamin ben benny", "matthew matt matty", "andrew andy drew",
    "steven steve stephen stevie", "edward ed eddie ted", "victoria vicky tori vic", "rebecca becky becca",
    "christine chris tina chrissy", "jessica jess jessie", "gabriel gabe", "isabel isabella izzy bella",
    "sofia sophia sophie", "theodore theo ted teddy", "abigail abby", "olivia liv", "zachary zach zack",
    "kenneth ken kenny", "timothy tim timmy", "gregory greg", "jeffrey jeff", "lawrence larry", "ronald ron ronnie",
    "donald don donnie", "charles charlie chuck chas", "frederick fred freddie", "jacqueline jackie",
    "josephine jo josie", "pamela pam", "cynthia cindy", "dorothy dot dottie", "barbara barb barbie",
    "angela angie", "valerie val", "miguel mike", "jose pepe", "francisco frank paco", "frances fran frankie",
    "francis frank", "luis lou", "aisha aysha ayesha", "mohammed mohammad muhammad mohamed",
]
NICK = {}
for i, g in enumerate(_NICK_GROUPS):
    for n in g.split():
        NICK.setdefault(n, set()).add(i)

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "rn", "lpn", "cna", "phd"}


def parse_name(full=None, first=None, last=None) -> dict | None:
    if first is not None or last is not None:
        f = simplify(first or "").split()
        l = simplify(last or "").split()
        l = [t for t in l if t not in SUFFIXES]
        if not f and not l:
            return None
        return {"first": f[0] if f else "", "middle": " ".join(f[1:]), "last": "".join(l),
                "display": f"{(first or '').strip().title()} {(last or '').strip().title()}".strip()}
    if is_empty(full):
        return None
    raw = str(full).strip()
    if "," in raw:  # LAST, FIRST M
        last_part, first_part = raw.split(",", 1)
    else:
        toks = raw.split()
        if len(toks) == 1:
            first_part, last_part = "", toks[0]
        else:
            # strip trailing suffixes before taking last token
            while len(toks) > 2 and simplify(toks[-1]) in SUFFIXES:
                toks = toks[:-1]
            first_part, last_part = " ".join(toks[:-1]), toks[-1]
    p = parse_name(first=first_part, last=last_part)
    if p:
        p["display"] = f"{first_part.strip().title()} {last_part.strip().title()}".strip()
    return p


def name_key(p: dict) -> str:
    """Grouping key that treats nicknames as equal (for clustering unmatched records)."""
    f = p["first"]
    groups = NICK.get(f)
    if groups:
        f = f"#{min(groups)}"
    return f"{p['last']}|{f}"


def name_similarity(a: dict, b: dict) -> tuple[float, str]:
    if not a or not b:
        return 0.0, "none"
    fa, fb, la, lb = a["first"], b["first"], a["last"], b["last"]
    if la == lb and fa == fb:
        return 1.0, "exact name"
    if la == lb and NICK.get(fa, set()) & NICK.get(fb, set()):
        return 0.95, "nickname"
    if la == lb and fa and fb and (len(fa) == 1 or len(fb) == 1) and fa[0] == fb[0]:
        return 0.85, "initial"
    if fa == lb and la == fb:
        return 0.9, "first/last swapped"
    if la == lb and fa and fb and (fa.startswith(fb) or fb.startswith(fa)) and min(len(fa), len(fb)) >= 3:
        return 0.9, "shortened first name"
    ls = SequenceMatcher(None, la, lb).ratio()
    fs = 1.0 if (fa == fb or NICK.get(fa, set()) & NICK.get(fb, set())) else SequenceMatcher(None, fa, fb).ratio()
    return round(0.6 * ls + 0.4 * fs, 3), "fuzzy"


# ---------------------------------------------------------------- dispatcher
def normalize_value(raw, spec: dict, refs: dict):
    t = spec.get("type", "text")
    if t == "date":
        return norm_date(raw)
    if t == "number":
        return norm_number(raw, spec.get("min"), spec.get("max"))
    if t == "phone":
        return norm_phone(raw)
    if t == "id":
        return norm_id(raw)
    if t == "license":
        return norm_license(raw)
    if t == "code":
        return norm_code(raw, refs[spec["ref"]])
    if t == "person_name":
        p = parse_name(full=raw)
        return (p, None) if p or is_empty(raw) else (None, f"'{raw}' is not a usable name")
    if is_empty(raw):
        return None, None
    return re.sub(r"\s+", " ", str(raw)).strip(), None
