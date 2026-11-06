"""Smart Merge — standalone WoS + Scopus deduplication / merge algorithm.

Pure Python (pandas + numpy only). Same matching logic as the main BibexPy v2
package: normalize -> block -> staged match (negative rules, DOI, PMID,
title+year+surname, journal+volume+page, borderline) -> field merge with fixed
per-field source preferences.

DOI is determinative: two records whose normalized DOIs differ are never the
same publication (no auto-merge, no borderline). The one exception is a DOI that
two indexers wrote with different separators ("jogh.12-05057" vs
"JOGH.12.05057"): that only lifts the veto, the DOI is not counted as evidence,
and the pair must still match on PMID, title+year+surname or
journal+volume+pages.

This is a vendored copy of the canonical algorithm in the main BibexPy package
(apps/api/services/smart_merger.py). Keep the two in sync — the algorithm is the
single source of truth; this file only strips the web/storage layer so the
merge can run headless (terminal / Colab) on plain DataFrames.

Record keys: every final record gets a sequential integer key in the UID column
(1..N, in the order merged pairs, unmatched WoS, unmatched Scopus), identical to
the numbering BibexPy 2.4 assigns on the same input. Source rows carry temporary
ids ("w0".., "s0"..) only while the merge runs; the final keys are assigned once,
and Borderline_Uncertain (wos_uid/scp_uid) and the Lost_* frames refer to them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from .cr_normalize import count_refs, normalize_cr


# ════════════════════════════════════════════════════════════════════════
#  String similarity (vendored from the main package)
# ════════════════════════════════════════════════════════════════════════

def normalize_name(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    match_dist = max(len(s1), len(s2)) // 2 - 1
    if match_dist < 0:
        match_dist = 0
    s1_matches = [False] * len(s1)
    s2_matches = [False] * len(s2)
    matches = 0
    for i, c in enumerate(s1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len(s2))
        for j in range(start, end):
            if s2_matches[j] or s2[j] != c:
                continue
            s1_matches[i] = s2_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    t = 0
    k = 0
    for i in range(len(s1)):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            t += 1
        k += 1
    t /= 2
    return (matches / len(s1) + matches / len(s2) + (matches - t) / matches) / 3


def jaro_winkler(s1: str, s2: str, p: float = 0.1) -> float:
    j = jaro(s1, s2)
    prefix = 0
    for c1, c2 in zip(s1[:4], s2[:4]):
        if c1 == c2:
            prefix += 1
        else:
            break
    return j + prefix * p * (1 - j)


def name_initials(full: str) -> tuple[str, str]:
    raw = str(full or "")
    if "," in raw:
        surname_part, _, given_part = raw.partition(",")
        surname = normalize_name(surname_part)
        initials = "".join(t[0] for t in normalize_name(given_part).split() if t)
        if surname:
            return surname, initials
    parts = normalize_name(raw).split()
    if not parts:
        return "", ""
    return parts[0], "".join(p[0] for p in parts[1:] if p)


# ════════════════════════════════════════════════════════════════════════
#  Field preferences + thresholds
# ════════════════════════════════════════════════════════════════════════

# BibexPy's record key column (sequential int64 1..N, assigned once per run)
UID_COL = "UID"

FIELD_PREFERENCES: dict[str, str] = {
    "TC": "wos",          # times cited
    "CR": "wos",          # cited references
    "NR": "wos",          # number of references
    # Row identity: while matching, every source row carries a TEMPORARY id ("w3" / "s7",
    # never persisted); a matched pair keeps the WoS row's id (merge_pair_with_preferences
    # handles it without logging a conflict). The final sequential keys 1..N are assigned
    # when the final frame is assembled (_assign_final_keys).
    "UID": "wos",
    "AB": "scopus",       # abstract
    "AU": "scopus",       # author short
    "AF": "scopus",       # author full
    "C1": "scopus",       # affiliations
    "DE": "union",        # author keywords
    "ID": "union",        # keywords plus
    "WC": "cross_fill_wos_first",
    "SC": "cross_fill_wos_first",
}
DEFAULT_PREFERENCE = "wos_first"

TITLE_EXACT_THRESHOLD = 0.92
TITLE_BORDERLINE_LOW = 0.80
YEAR_TOLERANCE = 1
JOURNAL_SIMILARITY = 0.90

STOPWORDS: set[str] = {
    "the", "a", "an", "of", "in", "on", "and", "or", "for", "to", "with",
    "by", "from", "as", "at", "is", "are", "was", "were", "be", "been",
}

_DOI_PREFIX_RE = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)
# Underscore + the Unicode dash family (hyphen, non-breaking hyphen, figure/en/
# em/horizontal dash) fold to one canonical '-'.
_DOI_SEP_RE = re.compile(r"[_\u2010-\u2015]")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")
_LATEX_RE = re.compile(r"\\[a-z]+\{[^}]*\}|\\[\\\\&%$#_{}~^]")
_ISSN_RE = re.compile(r"[^0-9Xx]")


# ════════════════════════════════════════════════════════════════════════
#  Normalize
# ════════════════════════════════════════════════════════════════════════

def _to_str(v: Any) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def _present(v: Any) -> bool:
    """Is this IDENTITY value really filled in? (None / NaN / "" / "nan" -> False)

    "Empty" takes three shapes depending on the pandas version: None (object
    dtype), NaN (pandas 3's `str` dtype — and NaN is TRUTHY), and "" / "nan"
    (an xlsx round-trip). A bare `if v` accepts NaN as a real identifier and
    therefore treats all identifier-less records as equal to one another, which
    collapses them onto a single key and deletes every one but the richest.
    Identity comparisons (DOI/PMID/UT/ISSN) use ONLY this helper.
    """
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s != "" and s.lower() not in ("nan", "none", "null")


def _clean_num(v: Any) -> str:
    """Comparison form for numeric fields (VL/BP/PG): '100.0' -> '100'."""
    s = _to_str(v)
    if re.fullmatch(r"\d+\.0+", s):
        return s.split(".", 1)[0]
    return s


def normalize_doi(raw: Any) -> Optional[str]:
    s = _to_str(raw)
    if not s:
        return None
    s = s.lower()
    # Dirty data can stack prefixes ('doi:', double 'https://doi.org/'); strip
    # until stable.
    for _ in range(4):
        prev = s
        s = _DOI_PREFIX_RE.sub("", s)
        s = re.sub(r"^doi\s*:\s*", "", s)
        s = re.sub(r"^doi\.org/", "", s)
        if s == prev:
            break
    s = s.rstrip("/. \t")
    # Separator folding: DOIs are compared in CANONICAL form because indexers
    # render separators differently — WoS lists an article as
    # '10.4103/jgid.jgid_12_19', Scopus as '10.4103/jgid.jgid-12-19'. Without
    # folding the pair is not merely missed: doi_conflict() vetoes it outright.
    s = _DOI_SEP_RE.sub("-", s)
    if not s.startswith("10."):
        return None
    return s


def normalize_title(raw: Any) -> str:
    s = _to_str(raw)
    if not s:
        return ""
    s = _LATEX_RE.sub(" ", s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    tokens = [t for t in s.split() if t not in STOPWORDS]
    return " ".join(tokens)


def normalize_year(raw: Any) -> Optional[int]:
    s = _to_str(raw)
    if not s:
        return None
    try:
        n = int(float(s))
        if 1900 <= n <= 2100:
            return n
        return None
    except (ValueError, TypeError):
        return None


def normalize_author_surname(raw: Any) -> str:
    s = _to_str(raw)
    if not s:
        return ""
    first = re.split(r"[;|]", s, maxsplit=1)[0]
    first = first.replace(",", " ")
    surname, _initials = name_initials(first)
    return surname.upper()


def normalize_issn(raw: Any) -> Optional[str]:
    s = _to_str(raw)
    if not s:
        return None
    s = _ISSN_RE.sub("", s.upper())
    if len(s) == 8:
        return s
    return None


def normalize_id_token(raw: Any) -> Optional[str]:
    s = _to_str(raw)
    if not s:
        return None
    s = s.lower().strip()
    return s or None


# ════════════════════════════════════════════════════════════════════════
#  Blocking
# ════════════════════════════════════════════════════════════════════════

def build_blocks(df: pd.DataFrame) -> dict[tuple[Optional[int], str], list[int]]:
    blocks: dict[tuple[Optional[int], str], list[int]] = {}
    for idx, row in df.iterrows():
        year = row.get("_norm_year")
        surname = row.get("_norm_surname", "")
        first_letter = surname[0] if surname else ""
        key = (year, first_letter)
        blocks.setdefault(key, []).append(int(idx))
    return blocks


# ════════════════════════════════════════════════════════════════════════
#  Multi-stage matching
# ════════════════════════════════════════════════════════════════════════

_DOI_SEP_CHARS = frozenset("-._")


def _doi_sep_profile(suffix: str) -> tuple[str, dict[int, str]]:
    """Split a DOI suffix into (separator-free core, position -> separators)."""
    core: list[str] = []
    gaps: dict[int, str] = {}
    for ch in suffix:
        if ch in _DOI_SEP_CHARS:
            gaps[len(core)] = gaps.get(len(core), "") + ch
        else:
            core.append(ch)
    return "".join(core), gaps


def doi_separator_variants(canon_a: Optional[str], canon_b: Optional[str]) -> bool:
    """Do two CANONICAL DOIs differ only in how their separators were written?

    The DOI-determinative rule stays: records whose normalized DOIs differ are
    different publications. The one exception is an indexer writing the same
    DOI differently - '10.7189/jogh.12-05057' and '10.7189/JOGH.12.05057' are
    the same article. Such a pair is not vetoed, but the DOI is NOT evidence:
    the pair has to earn its match in the later stages (never Stage 1).

    The rule is deliberately narrow. Deleting every separator and comparing
    the rest equates real, DIFFERENT papers: in Physical Review the
    volume|page boundary shifts - '.../physrevc.5.350' (Michaud 1972, vol 5,
    p 350) and '.../physrevc.53.50' (Awasthi 1996, vol 53, p 50) are both
    registered. So:
      * the registrant prefix (before the first '/') must be identical,
      * the separator-free core must be identical,
      * every differing separator position must be a 1:1 substitution
        (- / . / _) or an insertion/deletion at an inner boundary with at
        least one non-digit neighbour.
    Insertion/deletion between two digits, at either edge, and '--' vs '-'
    are rejected.
    """
    if not canon_a or not canon_b or canon_a == canon_b:
        return False
    pa, _, sa = canon_a.partition("/")
    pb, _, sb = canon_b.partition("/")
    if pa != pb or not sa or not sb:
        return False
    ca, ga = _doi_sep_profile(sa)
    cb, gb = _doi_sep_profile(sb)
    if ca != cb:
        return False
    for i in set(ga) | set(gb):
        x, y = ga.get(i, ""), gb.get(i, "")
        if x == y:
            continue
        if len(x) == 1 and len(y) == 1:
            continue                      # 1:1 substitution: safe
        if len(x) > 1 or len(y) > 1:
            return False                  # '--' vs '-'
        if i == 0 or i == len(ca):
            return False                  # insertion/deletion at an edge
        if ca[i - 1].isdigit() and ca[i].isdigit():
            return False                  # digit|digit: segmentation shift
    return True


def doi_conflict(raw_a: Any, raw_b: Any) -> bool:
    """True if both raw DOIs normalize to present-but-different values.

    A pair that differs only in separator spelling is not a conflict.
    """
    a = normalize_doi(raw_a)
    b = normalize_doi(raw_b)
    if not a or not b or a == b:
        return False
    return not doi_separator_variants(a, b)


def compute_match(w: dict, s: dict) -> Optional[dict]:
    """5-stage matching decision.

    Returns: None (no match) or {stage, stage_label, confidence, reason, doi, doi_variant,
    jw_title, year_diff, surname_match}
    """
    # ── Identifier HIERARCHY: DOI > PMID > ISSN ───────────────────────────
    # A DEFINITE equality at a higher level overrides a contradiction at a lower
    # level. E.g. WoS gives the print-ISSN, Scopus the e-ISSN — an ISSN
    # contradiction cannot veto a pair with the same DOI (old behavior:
    # negative_rule_check applied all levels at once and silently left the
    # same-DOI pair as a duplicate). A lower-level contradiction rejects only
    # when there is NO equality at a higher level.

    # DOI: conflict → never the same publication; equality → definite merge
    w_doi = w.get("_norm_doi")
    s_doi = s.get("_norm_doi")
    doi_note = ""
    doi_variant = False
    if _present(w_doi) and _present(s_doi) and w_doi != s_doi:
        if not doi_separator_variants(w_doi, s_doi):
            return None
        # Separator/spelling difference: the veto is lifted, the DOI is not counted
        # as evidence. The pair must go through the title/year/author stages
        # below — Stage 1 is not produced.
        doi_variant = True
        doi_note = f" | DOI spelling variant: {w_doi} <-> {s_doi}"
    elif _present(w_doi) and _present(s_doi):
        return {
            "stage": "1_doi_exact",
            "stage_label": "DOI exact",
            "confidence": 1.00,
            "reason": f"DOI exact: {w_doi}",
            "doi": w_doi,
            "doi_variant": False,
            "jw_title": None,
            "year_diff": None,
            "surname_match": None,
        }

    # PMID (if the DOI could not be compared): conflict → reject; equality → merge
    # (UT is not the same across databases, only PMID)
    w_pmid = w.get("_norm_pmid")
    s_pmid = s.get("_norm_pmid")
    if _present(w_pmid) and _present(s_pmid):
        if w_pmid != s_pmid:
            return None
        return {
            "stage": "2_pmid_exact",
            "stage_label": "PMID exact",
            "confidence": 0.99,
            "reason": f"PMID exact: {w_pmid}",
            "doi": "",
            "doi_variant": doi_variant,
            "jw_title": None,
            "year_diff": None,
            "surname_match": None,
        }

    # ISSN (journal-level safeguard): vetoes only the title stages
    w_issn = w.get("_norm_issn")
    s_issn = s.get("_norm_issn")
    if _present(w_issn) and _present(s_issn) and w_issn != s_issn:
        return None

    # Stage 3 — Title JW ≥ 0.92 + Year ±1 + Surname match
    w_title = w.get("_norm_title", "")
    s_title = s.get("_norm_title", "")
    if w_title and s_title:
        jw_title = jaro_winkler(w_title, s_title)
        w_year = w.get("_norm_year")
        s_year = s.get("_norm_year")
        year_diff = abs((w_year or 0) - (s_year or 0)) if (w_year is not None and s_year is not None) else None
        w_surname = w.get("_norm_surname", "")
        s_surname = s.get("_norm_surname", "")
        surname_match = bool(w_surname and s_surname and w_surname == s_surname)

        # Generic/short title safeguard: titles such as "Editorial", "Erratum",
        # "Preface" can belong to MORE THAN ONE different publication by the same
        # author in the same year (a journal's editor writes an editorial for
        # every issue). Such titles cannot automatically enter Stage 3; they are
        # merged only via an identifier (DOI/PMID) or Stage 4
        # (journal+volume+page).
        informative_title = len(w_title.split()) >= 3 or len(w_title) >= 15

        if (
            informative_title
            and jw_title >= TITLE_EXACT_THRESHOLD
            and year_diff is not None
            and year_diff <= YEAR_TOLERANCE
            and surname_match
        ):
            return {
                "stage": "3_title_year_surname",
                "stage_label": "Title+Year+Surname",
                "confidence": 0.95,
                "reason": f"JW(title)={jw_title:.3f} >= {TITLE_EXACT_THRESHOLD}, year_diff={year_diff}, surname '{w_surname}' matched" + doi_note,
                "doi": "",
                "doi_variant": doi_variant,
                "jw_title": round(jw_title, 4),
                "year_diff": year_diff,
                "surname_match": surname_match,
            }

        # Stage 4 — Journal + Volume + (Pages or BP) — for old publications without a DOI
        w_journal = w.get("_norm_journal", "")
        s_journal = s.get("_norm_journal", "")
        if w_journal and s_journal:
            jw_journal = jaro_winkler(w_journal, s_journal)
            # The xlsx path turns numeric fields into floats ("100.0"); WoS txt
            # gives strings ("100"). Without stripping the ".0" tail, the
            # comparison would always miss.
            w_vol = _clean_num(w.get("VL", ""))
            s_vol = _clean_num(s.get("VL", ""))
            w_bp = _clean_num(w.get("BP", ""))
            s_bp = _clean_num(s.get("BP", ""))
            w_pg = _clean_num(w.get("PG", ""))
            s_pg = _clean_num(s.get("PG", ""))
            page_match = (w_bp and s_bp and w_bp == s_bp) or (w_pg and s_pg and w_pg == s_pg)
            if (
                jw_journal >= JOURNAL_SIMILARITY
                and w_vol and s_vol and w_vol == s_vol
                and page_match
            ):
                return {
                    "stage": "4_journal_vol_page",
                    "stage_label": "Journal+Vol+Pages",
                    "confidence": 0.90,
                    "reason": f"JW(journal)={jw_journal:.3f}, vol={w_vol}, page_match=True" + doi_note,
                    "doi": "",
                    "doi_variant": doi_variant,
                    "jw_title": round(jw_title, 4),
                    "year_diff": year_diff,
                    "surname_match": surname_match,
                }

        # Stage 5 — Borderline (manual queue)
        if TITLE_BORDERLINE_LOW <= jw_title < TITLE_EXACT_THRESHOLD:
            # Confidence linear scale 0.70-0.85
            conf = 0.70 + (jw_title - TITLE_BORDERLINE_LOW) * (0.85 - 0.70) / (TITLE_EXACT_THRESHOLD - TITLE_BORDERLINE_LOW)
            return {
                "stage": "5_borderline",
                "stage_label": "Borderline (manual review)",
                "confidence": round(conf, 3),
                "reason": f"JW(title)={jw_title:.3f}, year_diff={year_diff}, surname_match={surname_match}" + doi_note,
                "doi": "",
                "doi_variant": doi_variant,
                "jw_title": round(jw_title, 4),
                "year_diff": year_diff,
                "surname_match": surname_match,
            }

    return None


# ════════════════════════════════════════════════════════════════════════
#  Field merge
# ════════════════════════════════════════════════════════════════════════

def _is_empty(v: Any) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s == "" or s.lower() == "nan"


def _union_values(w_val: Any, s_val: Any, sep: str = "; ") -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for v in (w_val, s_val):
        if _is_empty(v):
            continue
        for token in re.split(r"\s*[;|]\s*", _to_str(v)):
            token = token.strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            parts.append(token)
    return sep.join(parts)


def _apply_preference(field: str, w_val: Any, s_val: Any) -> tuple[Any, str]:
    pref = FIELD_PREFERENCES.get(field, DEFAULT_PREFERENCE)
    w_empty = _is_empty(w_val)
    s_empty = _is_empty(s_val)
    if w_empty and s_empty:
        return "", "empty"
    if pref == "wos":
        return (w_val, "wos") if not w_empty else (s_val, "scopus_fallback")
    if pref == "scopus":
        return (s_val, "scopus") if not s_empty else (w_val, "wos_fallback")
    if pref == "union":
        return _union_values(w_val, s_val), "union"
    if pref == "cross_fill_wos_first":
        if not w_empty:
            return w_val, "wos"
        return s_val, "scopus"
    if pref == "wos_first":
        if not w_empty:
            return w_val, "wos"
        return s_val, "scopus"
    if not w_empty:
        return w_val, "wos"
    return s_val, "scopus"


_INT_LIKE_RE = re.compile(r"\d+(\.0+)?")


def _same_for_conflict(w_val: Any, s_val: Any) -> bool:
    """Conflict-log equality: case/space-insensitive text, or the same whole number.

    A WoS volume that went through an xlsx round trip is '154.0' while Scopus has '154';
    those are the same value, not a conflict. Only which rows are LOGGED depends on this;
    the chosen/merged value never does.
    """
    a = _to_str(w_val).lower().strip()
    b = _to_str(s_val).lower().strip()
    if a == b:
        return True
    if _INT_LIKE_RE.fullmatch(a) and _INT_LIKE_RE.fullmatch(b):
        return _clean_num(a) == _clean_num(b)
    return False


def merge_pair_with_preferences(
    pair_id: str,
    w: dict,
    s: dict,
    all_columns: list[str],
) -> tuple[dict, list[dict]]:
    """Merge two records and log the conflicts.

    Returns: (merged_row, conflicts_list)
    """
    merged: dict[str, Any] = {}
    conflicts: list[dict] = []

    for col in all_columns:
        if col.startswith("_norm_"):
            continue  # internal columns
        if col == UID_COL:
            # A matched pair keeps the WoS row's identity; the Scopus row's is retired.
            # The two always differ, so this is identity bookkeeping and never a
            # field conflict (conflict_log.xlsx and the source distribution stay unchanged).
            w_uid, s_uid = w.get(col), s.get(col)
            merged[col] = s_uid if _is_empty(w_uid) else w_uid
            continue
        w_val = w.get(col)
        s_val = s.get(col)
        chosen, source = _apply_preference(col, w_val, s_val)
        merged[col] = chosen

        # Conflict log: both sides are filled AND the values differ
        if not _is_empty(w_val) and not _is_empty(s_val):
            if not _same_for_conflict(w_val, s_val):
                conflicts.append({
                    "pair_id": pair_id,
                    "field": col,
                    "wos_value": _to_str(w_val)[:200],
                    "scopus_value": _to_str(s_val)[:200],
                    "chosen_source": source,
                    "chosen_value": _to_str(chosen)[:200],
                    "preference_rule": FIELD_PREFERENCES.get(col, DEFAULT_PREFERENCE),
                })

    # DB labeling — merged record
    merged["DB"] = "BIBEXPY_SMART"
    merged["DB_Original"] = "ISI; SCOPUS"

    return merged, conflicts


# ════════════════════════════════════════════════════════════════════════
#  Orchestrator
# ════════════════════════════════════════════════════════════════════════

@dataclass
class SmartMergeResult:
    merged: pd.DataFrame          # final deduplicated dataset (UID = sequential key 1..N, first column)
    borderline: pd.DataFrame      # uncertain pairs kept separate (wos_uid/scp_uid = their UIDs in merged)
    conflicts: pd.DataFrame       # field-level conflicts resolved during merge
    lost_wos: pd.DataFrame        # WoS records with no match
    lost_scopus: pd.DataFrame     # Scopus records with no match
    stats: dict                   # summary counts
    match_stages: dict            # {stage_label: count} for definite matches


def _add_norm_columns(df: pd.DataFrame) -> None:
    n = len(df)
    # Columns are built from a LIST with dtype=object — never `.apply()`.
    # Under pandas 3 a text-returning apply() yields a `str` dtype column and
    # turns the None that means "empty" into NaN. NaN is truthy, so every
    # identity guard accepts it: all identifier-less rows of a source collapse
    # onto one key and only the richest survives (SILENT DATA LOSS), and such
    # records can no longer match anything. `astype(object)` does NOT fix this:
    # NaN stays NaN inside object dtype and stays truthy.
    def _col(src: str, fn) -> pd.Series:
        raw = df[src] if src in df.columns else pd.Series([""] * n, index=df.index)
        return pd.Series([fn(v) for v in raw], index=df.index, dtype=object)

    df["_norm_doi"] = _col("DI", normalize_doi)
    df["_norm_title"] = _col("TI", normalize_title)
    df["_norm_year"] = _col("PY", normalize_year)
    df["_norm_surname"] = _col("AU", normalize_author_surname)
    df["_norm_issn"] = _col("SN", normalize_issn)
    df["_norm_pmid"] = _col("PM", normalize_id_token)
    df["_norm_ut"] = _col("UT", normalize_id_token)
    df["_norm_journal"] = _col("SO", normalize_title)


def dedup_within_source(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop identifier-duplicates WITHIN one source (same UT/EID or same DOI).

    Overlapping export files can bring the same record twice; cross-source
    matching only compares WoS vs Scopus, so these survived (and a leftover
    same-DOI copy also defeats greedy one-to-one assignment). The richest row
    (most non-empty cells) per identifier group is kept.
    """
    if df.empty:
        return df, 0
    # Richness is accumulated column by column: df.astype(str) would materialize
    # a string copy of the whole frame at once (hundreds of MB on a large
    # corpus); the per-column sum gives the same score with a far lower peak.
    richness = pd.Series(0, index=df.index, dtype="int64")
    for col in df.columns:
        s = df[col]
        richness += s.notna().astype("int64") + (s.astype(str) != "").astype("int64")
    drop: set = set()
    for key_col in ("_norm_ut", "_norm_doi"):
        if key_col not in df.columns:
            continue
        best_for: dict[str, Any] = {}
        for idx in df.index:
            if idx in drop:
                continue
            v = df.at[idx, key_col]
            if not _present(v):
                continue
            prev = best_for.get(v)
            if prev is None:
                best_for[v] = idx
            elif richness[idx] > richness[prev]:
                drop.add(prev)
                best_for[v] = idx
            else:
                drop.add(idx)
    if not drop:
        return df, 0
    return df.loc[~df.index.isin(drop)], len(drop)


def generate_candidates(
    wos_df: pd.DataFrame, scp_df: pd.DataFrame,
) -> list[tuple[float, int, int, dict]]:
    """Candidate (WoS, Scopus) pairs — IDENTIFIER-FIRST, then blocking.

    Any pair sharing a normalized DOI (or PMID) is evaluated REGARDLESS of
    blocks. Blocking-only candidate generation silently skipped same-DOI pairs
    whose surname parsed differently across sources ("RAHIM ZA" vs
    "ABDUL RAHIM NR") or whose years differed (early access vs print), so
    DOI-exact never got a chance and duplicates survived. Blocking now only
    scopes the title-similarity stages.
    """
    candidates: list[tuple[float, int, int, dict]] = []
    seen_pairs: set[tuple[int, int]] = set()

    def consider(w_idx: int, s_idx: int) -> None:
        pair = (int(w_idx), int(s_idx))
        if pair in seen_pairs:
            return
        seen_pairs.add(pair)
        m = compute_match(wos_df.loc[w_idx].to_dict(), scp_df.loc[s_idx].to_dict())
        if m is not None:
            candidates.append((m["confidence"], pair[0], pair[1], m))

    for key_col in ("_norm_doi", "_norm_pmid"):
        if key_col not in wos_df.columns or key_col not in scp_df.columns:
            continue
        w_index: dict[str, list[int]] = {}
        for idx in wos_df.index:
            v = wos_df.at[idx, key_col]
            if _present(v):
                w_index.setdefault(v, []).append(int(idx))
        for idx in scp_df.index:
            v = scp_df.at[idx, key_col]
            if not _present(v):
                continue
            for w_idx in w_index.get(v, ()):
                consider(w_idx, int(idx))

    # Blocking scopes the title stages. The block key wants the EXACT year,
    # so Stage 3's +-1 year tolerance never fired: early-access vs print-year
    # pairs were not even candidates. Each WoS block is now also compared with
    # the Scopus blocks of the neighbouring years (one-sided: ~3x candidates,
    # not 9x). Records without a year keep the old behaviour.
    wos_blocks = build_blocks(wos_df)
    scp_blocks = build_blocks(scp_df)
    for (year, letter), w_idxs in wos_blocks.items():
        years = (year,) if year is None else (year - 1, year, year + 1)
        for y in years:
            s_idxs = scp_blocks.get((y, letter))
            if not s_idxs:
                continue
            for w_idx in w_idxs:
                for s_idx in s_idxs:
                    consider(w_idx, s_idx)

    candidates.sort(key=lambda x: -x[0])
    return candidates


def _assign_source_ids(wos_df: pd.DataFrame, scp_df: pd.DataFrame) -> None:
    """Give every WoS and Scopus source row a TEMPORARY id, IN PLACE, before dedup and matching.

    The id lives in the UID column only while the merge runs ("w0", "w1", ... and "s0", "s1", ...,
    by row position, so the two sources can never collide). Any UID a re-imported export already
    carried is overwritten: the sequential keys 1..N (the same numbering BibexPy assigns) are assigned once, when the final
    frame is assembled (see :func:`_assign_final_keys`), and are the only keys that survive.
    """
    wos_df[UID_COL] = [f"w{i}" for i in range(len(wos_df))]
    scp_df[UID_COL] = [f"s{i}" for i in range(len(scp_df))]


def _assign_final_keys(
    final_df: pd.DataFrame,
    matches: list[dict],
    borderline: list[dict],
    wos_not_matched: pd.DataFrame,
    scp_not_matched: pd.DataFrame,
) -> int:
    """Assign the permanent sequential keys 1..N to the final frame and translate every reference.

    ``final_df`` is, in this order, the merged pairs (one row per entry of ``matches``), the
    unmatched WoS rows and the unmatched Scopus rows, each carrying the temporary id of its
    source row. All of it is rewritten IN PLACE:
      * final_df[UID] = 1..N in that order (int64);
      * every match gets ``uid`` = the key of its merged row (no temporary ids survive);
      * every borderline item's wos_uid/scp_uid = the final keys of its two rows (both rows are in
        the final dataset: a pending pair is two separate rows, or rows that were matched elsewhere);
      * the unmatched frames (Lost_WoS.xlsx / Lost_Scopus.xlsx) carry the final key of their row in UID.
    Returns N (the number of final records).
    """
    import numpy as np

    expected = ([m["wos_tmp"] for m in matches]
                + list(wos_not_matched[UID_COL]) + list(scp_not_matched[UID_COL]))
    if len(expected) != len(final_df) or list(final_df[UID_COL]) != expected:
        raise RuntimeError(
            "merge_uid_mismatch: the final frame is not (pairs, unmatched WoS, unmatched Scopus) in order"
        )
    temp_to_key: dict[str, int] = {}
    for i, m in enumerate(matches):
        key = i + 1
        temp_to_key[m["wos_tmp"]] = key
        temp_to_key[m["scp_tmp"]] = key
        m["uid"] = key
    pos = len(matches)
    for frame in (wos_not_matched, scp_not_matched):
        keys = []
        for temp in frame[UID_COL]:
            pos += 1
            temp_to_key[temp] = pos
            keys.append(pos)
        frame[UID_COL] = np.asarray(keys, dtype="int64")
    for b in borderline:
        b["wos_uid"] = temp_to_key[b["wos_uid"]]
        b["scp_uid"] = temp_to_key[b["scp_uid"]]
    final_df[UID_COL] = np.arange(1, len(final_df) + 1, dtype="int64")
    return int(len(final_df))


def _uid_first(df: pd.DataFrame) -> pd.DataFrame:
    """Return df with the UID column first (presentation only)."""
    if UID_COL not in df.columns:
        return df
    return df[[UID_COL] + [c for c in df.columns if c != UID_COL]]


def smart_merge(wos_df: pd.DataFrame, scp_df: pd.DataFrame) -> SmartMergeResult:
    """Merge + deduplicate a WoS and a Scopus DataFrame.

    Inputs are bibliographic frames with standard 2-letter columns (TI, DI, PY,
    AU, SO, VL, BP, ...). Returns a SmartMergeResult; nothing is written to disk.
    """
    wos_df = wos_df.copy().reset_index(drop=True)
    scp_df = scp_df.copy().reset_index(drop=True)
    wos_df["DB"] = "ISI"
    scp_df["DB"] = "SCOPUS"

    # Temporary row ids for matching; the permanent keys are assigned at the end (_assign_final_keys).
    _assign_source_ids(wos_df, scp_df)

    _add_norm_columns(wos_df)
    _add_norm_columns(scp_df)

    wos_raw_n, scp_raw_n = len(wos_df), len(scp_df)
    wos_df, intra_wos_removed = dedup_within_source(wos_df)
    scp_df, intra_scp_removed = dedup_within_source(scp_df)

    candidates = generate_candidates(wos_df, scp_df)

    matches: list[dict] = []
    borderline_rows: list[dict] = []
    matched_wos: set[int] = set()
    matched_scp: set[int] = set()
    stage_counts: dict[str, int] = {}
    pair_counter = 0

    for conf, w_idx, s_idx, m in candidates:
        if w_idx in matched_wos or s_idx in matched_scp:
            continue
        pair_counter += 1
        pair_id = f"p{pair_counter:06d}"
        if m["stage"] == "5_borderline":
            w_row = wos_df.loc[w_idx]
            s_row = scp_df.loc[s_idx]
            borderline_rows.append({
                "pair_id": pair_id,
                # Temporary source ids here, translated to the final keys of the two rows
                # by _assign_final_keys before the result is returned.
                "wos_uid": _to_str(w_row.get(UID_COL, "")),
                "scp_uid": _to_str(s_row.get(UID_COL, "")),
                "jw_title": m["jw_title"], "confidence": m["confidence"],
                "wos_title": _to_str(w_row.get("TI", ""))[:300],
                "scp_title": _to_str(s_row.get("TI", ""))[:300],
                "wos_doi": _to_str(w_row.get("DI", "")), "scp_doi": _to_str(s_row.get("DI", "")),
                "wos_year": w_row.get("_norm_year"), "scp_year": s_row.get("_norm_year"),
                "wos_journal": _to_str(w_row.get("SO", "")), "scp_journal": _to_str(s_row.get("SO", "")),
                "reason": m["reason"],
            })
        else:
            matched_wos.add(w_idx)
            matched_scp.add(s_idx)
            stage_counts[m["stage_label"]] = stage_counts.get(m["stage_label"], 0) + 1
            matches.append({"pair_id": pair_id,
                            # Temporary source ids; the final key is added as ``uid`` by _assign_final_keys.
                            "wos_tmp": _to_str(wos_df.at[w_idx, UID_COL]),
                            "scp_tmp": _to_str(scp_df.at[s_idx, UID_COL]),
                            "wos_index": int(w_idx), "scp_index": int(s_idx), **m})

    # First-seen order (WoS then Scopus): a set() would make the column order hash-seed dependent.
    all_columns = list(dict.fromkeys(c for cols in (wos_df.columns, scp_df.columns) for c in cols))
    conflicts: list[dict] = []
    merged_rows: list[dict] = []
    for match in matches:
        w_row = wos_df.loc[match["wos_index"]].to_dict()
        s_row = scp_df.loc[match["scp_index"]].to_dict()
        merged_row, pair_conflicts = merge_pair_with_preferences(match["pair_id"], w_row, s_row, all_columns)
        merged_rows.append(merged_row)
        conflicts.extend(pair_conflicts)

    wos_not_matched = wos_df.loc[~wos_df.index.isin(matched_wos)].copy()
    scp_not_matched = scp_df.loc[~scp_df.index.isin(matched_scp)].copy()

    def _drop_norm(df: pd.DataFrame) -> pd.DataFrame:
        cols = [c for c in df.columns if c.startswith("_norm_")]
        return df.drop(columns=cols, errors="ignore")

    merged_df = _drop_norm(pd.DataFrame(merged_rows)) if merged_rows else pd.DataFrame()
    wos_not_matched = _drop_norm(wos_not_matched)
    scp_not_matched = _drop_norm(scp_not_matched)

    final_df = pd.concat([merged_df, wos_not_matched, scp_not_matched], ignore_index=True)
    # Permanent sequential keys 1..N in the final row order; borderline wos_uid/scp_uid and the
    # unmatched frames are translated to them in place.
    _assign_final_keys(final_df, matches, borderline_rows, wos_not_matched, scp_not_matched)
    # Presentation only: show the key first.
    final_df = _uid_first(final_df)
    wos_not_matched = _uid_first(wos_not_matched)
    scp_not_matched = _uid_first(scp_not_matched)

    # CR normalization — Scopus-grammar cited references are rewritten into
    # WoS grammar. Otherwise VOSviewer / bibliometrix cannot match any reference
    # of the Scopus-sourced rows (Scopus separates authors AND references with
    # ';', so every reference is shredded into author fragments). normalize_cr
    # is idempotent; comma-less WoS rows pass through unchanged.
    cr_normalized = nr_filled = 0
    if "CR" in final_df.columns:
        # Cells as text, missing -> "" (no fillna downcast on a float CR column under pandas 3).
        before = pd.Series(["" if pd.isna(v) else str(v) for v in final_df["CR"].tolist()],
                           index=final_df.index, dtype=object)
        after = before.map(normalize_cr)
        filled_cr = before.str.strip() != ""
        changed = filled_cr & (after != before)
        cr_normalized = int(changed.sum())
        if changed.any():
            final_df["CR"] = final_df["CR"].astype(object)
            final_df.loc[changed, "CR"] = after[changed]
        # NR (reference count): count from CR when blank OR when CR was rewritten
        # here. Scopus exports have no NR, and on matched pairs whose CR fell back
        # to the Scopus side the WoS NR (often 0) no longer describes the string.
        if "NR" not in final_df.columns:
            final_df["NR"] = ""
        nr = final_df["NR"]
        blank = nr.isna() | nr.astype(str).str.strip().isin(("", "nan", "NaN", "None"))
        fill = (blank | changed) & filled_cr
        if fill.any():
            # object dtype: writing ints into a float column yields "NR 3.0".
            final_df["NR"] = final_df["NR"].astype(object)
            final_df.loc[fill, "NR"] = after[fill].map(count_refs)
            nr_filled = int(fill.sum())

    total_input = wos_raw_n + scp_raw_n
    duplicates = len(matches) + intra_wos_removed + intra_scp_removed
    stats = {
        "wos_input": int(wos_raw_n),
        "scopus_input": int(scp_raw_n),
        "total_input": int(total_input),
        "merged_count": int(len(final_df)),
        "duplicates_removed": int(duplicates),
        "dedup_rate": round(duplicates / total_input, 4) if total_input else 0.0,
        "matched_pairs": int(len(matches)),
        "intra_wos_removed": int(intra_wos_removed),
        "intra_scopus_removed": int(intra_scp_removed),
        "borderline_count": int(len(borderline_rows)),
        "lost_wos_count": int(len(wos_not_matched)),
        "lost_scopus_count": int(len(scp_not_matched)),
        "conflict_count": int(len(conflicts)),
        "cr_normalized": cr_normalized,
        "nr_filled": nr_filled,
    }

    return SmartMergeResult(
        merged=final_df,
        borderline=pd.DataFrame(borderline_rows),
        conflicts=pd.DataFrame(conflicts),
        lost_wos=wos_not_matched,
        lost_scopus=scp_not_matched,
        stats=stats,
        match_stages=stage_counts,
    )
