"""
Company-name normalisation.

Deterministic rules run first and do almost all of the work. Fuzzy matching
only ever sees what the rules could not settle. That ordering is the point of
the project, not an implementation detail: when an exact identifier exists,
inference is the wrong tool.
"""

from __future__ import annotations

import re

# Order matters. Longer forms are stripped before their abbreviations so that
# "CORPORATION" does not leave a dangling "ATION" behind a "CORP" rule.
_SUFFIXES = [
    "INCORPORATED", "CORPORATION", "COMPANY", "LIMITED", "HOLDINGS", "HOLDING",
    "PARTNERS", "PARTNERSHIP", "TRUST", "FUND", "GROUP", "PLC", "LLC", "LP",
    "INC", "CORP", "CO", "LTD", "NV", "SA", "AG", "SE", "AB", "OYJ", "ASA",
]

_EXPANSIONS = {
    "INTL": "INTERNATIONAL", "INTERNATL": "INTERNATIONAL",
    "TECH": "TECHNOLOGIES", "TECHS": "TECHNOLOGIES", "TECHNOLOGY": "TECHNOLOGIES",
    "IND": "INDUSTRIES", "INDS": "INDUSTRIES", "INDUSTRY": "INDUSTRIES",
    "SVCS": "SERVICES", "SVC": "SERVICES", "SERVICE": "SERVICES",
    "MFG": "MANUFACTURING", "PHARM": "PHARMACEUTICALS",
    "PHARMA": "PHARMACEUTICALS", "PHARMACEUTICAL": "PHARMACEUTICALS",
    "NATL": "NATIONAL", "FINL": "FINANCIAL", "FIN": "FINANCIAL",
    "RES": "RESOURCES", "SYS": "SYSTEMS", "SYSTEM": "SYSTEMS",
    "COMMS": "COMMUNICATIONS", "COMM": "COMMUNICATIONS",
    "BANCORPORATION": "BANCORP", "BK": "BANK",
}

# EDGAR appends these to disambiguate filer records. They are not part of the name.
_EDGAR_QUALIFIER = re.compile(r"/\s*[A-Z]{2,4}\s*(?:/|$)|\\\s*[A-Z]{2,4}\s*\\?$")
_SHARE_CLASS = re.compile(r"\bCL(?:ASS)?\s+[A-Z]\b|\bSERIES\s+[A-Z0-9]+\b")
_PAREN = re.compile(r"\([^)]*\)")
_NONALNUM = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")


def normalize(name: str | None) -> str:
    """
    Canonical form of a company name.

    Idempotent: normalize(normalize(x)) == normalize(x). The tests assert this,
    because a non-idempotent normaliser silently produces different keys
    depending on how many times the pipeline touched a row.
    """
    if not name:
        return ""
    s = name.upper()
    s = _EDGAR_QUALIFIER.sub(" ", s)
    s = _PAREN.sub(" ", s)
    s = s.replace("&", " AND ")
    s = _SHARE_CLASS.sub(" ", s)
    s = _NONALNUM.sub(" ", s)
    s = _WS.sub(" ", s).strip()

    tokens = [_EXPANSIONS.get(t, t) for t in s.split()]
    # Strip trailing legal suffixes repeatedly - "ACME HOLDINGS INC" -> "ACME"
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    # A leading "THE" carries no identity
    if tokens and tokens[0] == "THE":
        tokens.pop(0)
    return " ".join(tokens)


def same_normalized(a: str | None, b: str | None) -> bool:
    na, nb = normalize(a), normalize(b)
    return bool(na) and na == nb


def prefix_related(a: str | None, b: str | None) -> bool:
    """
    One normalised name is a whole-token prefix of the other.

    Catches holdco restructurings: "APACHE" vs "APACHE CORP" normalise apart
    only by suffix, but "FRESH MARKET" vs "FRESH MARKET HOLDINGS" needs this.
    Whole-token, so "AL" never matches "ALPHABET".
    """
    ta, tb = normalize(a).split(), normalize(b).split()
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return long_[:len(short)] == short
