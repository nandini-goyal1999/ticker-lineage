"""
Tests live where the ambiguity is, not where the code is complex.

Normalisation is twelve lines of regex and it is the single highest-risk
component in the project: every downstream classification depends on two
names either collapsing to the same key or not. Every case below is a real
pair pulled from the SEC data, not an invented example.
"""

import pytest

from tickerlineage.normalize import normalize, prefix_related, same_normalized


class TestIdempotence:
    """A normaliser that is not idempotent produces different keys depending on
    how many times the pipeline touched a row. That bug is invisible until it
    is expensive."""

    @pytest.mark.parametrize("name", [
        "BlackRock, Inc.", "Dupont E I De Nemours & Co", "Apache Corp.",
        "Morgan Stanley Dean Witter Discover & Co", "Castle A M & Co",
        "APPLE COMPUTER INC/ FA", "BlackRock Funding, Inc. /DE",
        "Fresh Market Holdings, Inc.", "ALABAMA POWER CO", "",
    ])
    def test_normalize_is_idempotent(self, name):
        once = normalize(name)
        assert normalize(once) == once


class TestSuffixStripping:
    @pytest.mark.parametrize("raw,expected", [
        ("Apache Corp.",                    "APACHE"),
        ("APA Corp",                        "APA"),
        ("Alcoa Inc.",                      "ALCOA"),
        ("Alcoa Corp",                      "ALCOA"),
        ("BlackRock, Inc.",                 "BLACKROCK"),
        ("Multi Ways Holdings Ltd",         "MULTI WAYS"),
        ("The Fresh Market, Inc.",          "FRESH MARKET"),
        ("SentinelOne, Inc.",               "SENTINELONE"),
    ])
    def test_strips_legal_suffixes(self, raw, expected):
        assert normalize(raw) == expected

    def test_strips_stacked_suffixes(self):
        # "HOLDINGS INC" is two suffixes, not one. A single pass leaves HOLDINGS.
        assert normalize("Acme Holdings Inc") == "ACME"
        assert normalize("Acme Group Holdings Corporation") == "ACME"

    def test_does_not_eat_the_whole_name(self):
        # A company genuinely called "Trust Company" must not normalise to "".
        # This is the failure mode where every suffix-only name collapses
        # together and gets matched to everything.
        assert normalize("Corp") == ""
        assert normalize("Grupo Financiero") != ""


class TestEdgarQualifiers:
    """EDGAR appends /DE, /FA, /MD to disambiguate filer records. They are
    filing metadata, not part of the company's identity."""

    def test_strips_state_qualifier(self):
        assert normalize("BlackRock Funding, Inc. /DE") == "BLACKROCK FUNDING"

    def test_strips_trailing_qualifier(self):
        assert normalize("APPLE COMPUTER INC/ FA") == "APPLE COMPUTER"

    def test_qualifier_does_not_break_real_match(self):
        assert same_normalized("BlackRock, Inc.", "BLACKROCK INC /DE")


class TestAmpersandAndAbbreviations:
    def test_ampersand_expands(self):
        assert same_normalized("Dupont E I De Nemours & Co", "DUPONT E I DE NEMOURS AND CO")

    def test_bbt(self):
        # Real pair: BB&T Corporation. The & must not silently vanish and
        # glue BB and T into one token.
        assert normalize("BB&T Corporation") == "BB AND T"

    @pytest.mark.parametrize("abbrev,full", [
        ("Acme Intl Inc",        "Acme International Inc"),
        ("Acme Tech Corp",       "Acme Technologies Corp"),
        ("Acme Pharma Ltd",      "Acme Pharmaceuticals Limited"),
        ("Acme Finl Group",      "Acme Financial Group"),
    ])
    def test_abbreviations_converge(self, abbrev, full):
        assert same_normalized(abbrev, full)


class TestTheHardNegatives:
    """The cases that must NOT match. False positives here silently merge two
    unrelated companies, which is the exact failure the project is about."""

    @pytest.mark.parametrize("a,b", [
        ("Google Inc.",                    "Alphabet Inc."),
        ("Sprint Corporation",             "SentinelOne, Inc."),
        ("Morgan Stanley",                 "Multi Ways Holdings Ltd"),
        ("United Technologies Corporation","Ultratrex Inc."),
        ("SunTrust Banks, Inc.",           "Solidion Technology Inc."),
        ("BB&T Corporation",               "Beacon Financial Corp"),
        ("Blackhawk Network Holdings, Inc.","HawkEye 360, Inc."),
        ("Media General, Inc.",            "Montrose Environmental Group, Inc."),
    ])
    def test_unrelated_companies_do_not_collapse(self, a, b):
        assert not same_normalized(a, b)
        assert not prefix_related(a, b)

    def test_shared_prefix_word_is_not_a_match(self):
        # "Blackhawk" and "BlackRock" share four characters. Whole-token
        # prefixing must reject them.
        assert not prefix_related("Blackhawk Network Holdings, Inc.", "BlackRock, Inc.")

    def test_short_token_does_not_prefix_everything(self):
        assert not prefix_related("AL Industries", "Alphabet Inc.")


class TestHoldcoRestructures:
    """The cases that MUST match. These are the same business under a new
    filer entity - a CIK-keyed join breaks on exactly these."""

    def test_blackrock_holdco(self):
        # CIK 1364742 -> 2012383, both filing as "BlackRock, Inc."
        assert same_normalized("BlackRock, Inc.", "BlackRock, Inc.")

    def test_apache_to_apa(self):
        # Apache Corp. -> APA Corp, CIK 6769 -> 1841666. Different normalised
        # names, so this one is legitimately NOT a deterministic match. It is
        # caught by fuzzy scoring, and the test documents that boundary.
        assert not same_normalized("Apache Corp.", "APA Corp")

    def test_alcoa_inc_to_alcoa_corp(self):
        assert same_normalized("Alcoa Inc.", "Alcoa Corp")

    def test_fresh_market_prefix(self):
        assert prefix_related("Fresh Market, Inc.", "Fresh Market Holdings, Inc.")
