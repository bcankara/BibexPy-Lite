"""Smart Merge — standalone WoS + Scopus deduplication / merge algorithm.

Pure Python (pandas + numpy only). Same matching logic as the main BibexPy v2
package: normalize -> block -> staged match (negative rules, DOI, PMID,
title+year+surname, journal+volume+page, borderline) -> field merge with fixed
per-field source preferences.

DOI is determinative: two records whose normalized DOIs differ are never the
same publication (no auto-merge, no borderline).

This is a vendored copy of the canonical algorithm in the main BibexPy package
(apps/api/services/smart_merger.py). Keep the two in sync — the algorithm is the
single source of truth; this file only strips the web/storage layer so the
merge can run headless (terminal / Colab) on plain DataFrames.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd


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

FIELD_PREFERENCES: dict[str, str] = {
    "TC": "wos",          # times cited
    "CR": "wos",          # cited references
    "NR": "wos",          # number of references
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


def normalize_doi(raw: Any) -> Optional[str]:
    s = _to_str(raw)
    if not s:
        return None
    s = s.lower()
    s = _DOI_PREFIX_RE.sub("", s)
    s = s.rstrip("/. \t")
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

def negative_rule_check(w: dict, s: dict) -> Optional[str]:
    """Conflicting identifiers -> reject (never the same publication).

    DOI is determinative: if both records have a normalized DOI and they differ,
    they are different publications regardless of title/journal similarity (no
    auto-merge, never enters the borderline queue). The same conflict logic
    applies to PMID and ISSN.
    """
    for key in ("_norm_doi", "_norm_pmid", "_norm_issn"):
        wv = w.get(key)
        sv = s.get(key)
        if wv and sv and wv != sv:
            return f"{key.replace('_norm_', '').upper()} mismatch ({wv} != {sv})"
    return None


def doi_conflict(raw_a: Any, raw_b: Any) -> bool:
    """True if both raw DOIs normalize to present-but-different values."""
    a = normalize_doi(raw_a)
    b = normalize_doi(raw_b)
    return bool(a and b and a != b)


def compute_match(w: dict, s: dict) -> Optional[dict]:
    # Stage 0 — Negative rules (reject)
    if negative_rule_check(w, s):
        return None

    # Stage 1 — DOI exact
    w_doi = w.get("_norm_doi")
    s_doi = s.get("_norm_doi")
    if w_doi and s_doi and w_doi == s_doi:
        return {
            "stage": "1_doi_exact", "stage_label": "DOI exact", "confidence": 1.00,
            "reason": f"DOI exact: {w_doi}",
            "jw_title": None, "year_diff": None, "surname_match": None,
        }

    # Stage 2 — PMID exact
    w_pmid = w.get("_norm_pmid")
    s_pmid = s.get("_norm_pmid")
    if w_pmid and s_pmid and w_pmid == s_pmid:
        return {
            "stage": "2_pmid_exact", "stage_label": "PMID exact", "confidence": 0.99,
            "reason": f"PMID exact: {w_pmid}",
            "jw_title": None, "year_diff": None, "surname_match": None,
        }

    # Stage 3 — Title JW + Year +-1 + Surname
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

        if (
            jw_title >= TITLE_EXACT_THRESHOLD
            and year_diff is not None
            and year_diff <= YEAR_TOLERANCE
            and surname_match
        ):
            return {
                "stage": "3_title_year_surname", "stage_label": "Title+Year+Surname",
                "confidence": 0.95,
                "reason": f"JW(title)={jw_title:.3f} >= {TITLE_EXACT_THRESHOLD}, year_diff={year_diff}, surname='{w_surname}'",
                "jw_title": round(jw_title, 4), "year_diff": year_diff, "surname_match": surname_match,
            }

        # Stage 4 — Journal + Volume + (Pages or BP)
        w_journal = w.get("_norm_journal", "")
        s_journal = s.get("_norm_journal", "")
        if w_journal and s_journal:
            jw_journal = jaro_winkler(w_journal, s_journal)
            w_vol = _to_str(w.get("VL", ""))
            s_vol = _to_str(s.get("VL", ""))
            w_bp = _to_str(w.get("BP", ""))
            s_bp = _to_str(s.get("BP", ""))
            w_pg = _to_str(w.get("PG", ""))
            s_pg = _to_str(s.get("PG", ""))
            page_match = (w_bp and s_bp and w_bp == s_bp) or (w_pg and s_pg and w_pg == s_pg)
            if (
                jw_journal >= JOURNAL_SIMILARITY
                and w_vol and s_vol and w_vol == s_vol
                and page_match
            ):
                return {
                    "stage": "4_journal_vol_page", "stage_label": "Journal+Vol+Pages",
                    "confidence": 0.90,
                    "reason": f"JW(journal)={jw_journal:.3f}, vol={w_vol}, page_match=True",
                    "jw_title": round(jw_title, 4), "year_diff": year_diff, "surname_match": surname_match,
                }

        # Stage 5 — Borderline (manual review range)
        if TITLE_BORDERLINE_LOW <= jw_title < TITLE_EXACT_THRESHOLD:
            conf = 0.70 + (jw_title - TITLE_BORDERLINE_LOW) * (0.85 - 0.70) / (TITLE_EXACT_THRESHOLD - TITLE_BORDERLINE_LOW)
            return {
                "stage": "5_borderline", "stage_label": "Borderline (manual review)",
                "confidence": round(conf, 3),
                "reason": f"JW(title)={jw_title:.3f}, year_diff={year_diff}, surname_match={surname_match}",
                "jw_title": round(jw_title, 4), "year_diff": year_diff, "surname_match": surname_match,
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


def merge_pair_with_preferences(pair_id: str, w: dict, s: dict, all_columns: list[str]) -> tuple[dict, list[dict]]:
    merged: dict[str, Any] = {}
    conflicts: list[dict] = []
    for col in all_columns:
        if col.startswith("_norm_"):
            continue
        w_val = w.get(col)
        s_val = s.get(col)
        chosen, source = _apply_preference(col, w_val, s_val)
        merged[col] = chosen
        if not _is_empty(w_val) and not _is_empty(s_val):
            if _to_str(w_val).lower().strip() != _to_str(s_val).lower().strip():
                conflicts.append({
                    "pair_id": pair_id, "field": col,
                    "wos_value": _to_str(w_val)[:200], "scopus_value": _to_str(s_val)[:200],
                    "chosen_source": source, "chosen_value": _to_str(chosen)[:200],
                    "preference_rule": FIELD_PREFERENCES.get(col, DEFAULT_PREFERENCE),
                })
    merged["DB"] = "BIBEXPY_SMART"
    merged["DB_Original"] = "ISI; SCOPUS"
    return merged, conflicts


# ════════════════════════════════════════════════════════════════════════
#  Orchestrator
# ════════════════════════════════════════════════════════════════════════

@dataclass
class SmartMergeResult:
    merged: pd.DataFrame          # final deduplicated dataset (definite matches + unmatched)
    borderline: pd.DataFrame      # uncertain pairs kept separate (review manually)
    conflicts: pd.DataFrame       # field-level conflicts resolved during merge
    lost_wos: pd.DataFrame        # WoS records with no match
    lost_scopus: pd.DataFrame     # Scopus records with no match
    stats: dict                   # summary counts
    match_stages: dict            # {stage_label: count} for definite matches


def _add_norm_columns(df: pd.DataFrame) -> None:
    n = len(df)
    df["_norm_doi"] = df.get("DI", pd.Series([""] * n, index=df.index)).apply(normalize_doi)
    df["_norm_title"] = df.get("TI", pd.Series([""] * n, index=df.index)).apply(normalize_title)
    df["_norm_year"] = df.get("PY", pd.Series([""] * n, index=df.index)).apply(normalize_year)
    df["_norm_surname"] = df.get("AU", pd.Series([""] * n, index=df.index)).apply(normalize_author_surname)
    df["_norm_issn"] = df.get("SN", pd.Series([""] * n, index=df.index)).apply(normalize_issn)
    df["_norm_pmid"] = df.get("PM", pd.Series([""] * n, index=df.index)).apply(normalize_id_token)
    df["_norm_ut"] = df.get("UT", pd.Series([""] * n, index=df.index)).apply(normalize_id_token)
    df["_norm_journal"] = df.get("SO", pd.Series([""] * n, index=df.index)).apply(normalize_title)


def smart_merge(wos_df: pd.DataFrame, scp_df: pd.DataFrame) -> SmartMergeResult:
    """Merge + deduplicate a WoS and a Scopus DataFrame.

    Inputs are bibliographic frames with standard 2-letter columns (TI, DI, PY,
    AU, SO, VL, BP, ...). Returns a SmartMergeResult; nothing is written to disk.
    """
    wos_df = wos_df.copy().reset_index(drop=True)
    scp_df = scp_df.copy().reset_index(drop=True)
    wos_df["DB"] = "ISI"
    scp_df["DB"] = "SCOPUS"

    _add_norm_columns(wos_df)
    _add_norm_columns(scp_df)

    wos_blocks = build_blocks(wos_df)
    scp_blocks = build_blocks(scp_df)
    common_keys = set(wos_blocks.keys()) & set(scp_blocks.keys())

    candidates: list[tuple[float, int, int, dict]] = []
    for key in common_keys:
        for w_idx in wos_blocks[key]:
            w_row = wos_df.loc[w_idx].to_dict()
            for s_idx in scp_blocks[key]:
                s_row = scp_df.loc[s_idx].to_dict()
                m = compute_match(w_row, s_row)
                if m is not None:
                    candidates.append((m["confidence"], w_idx, s_idx, m))
    candidates.sort(key=lambda x: -x[0])

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
            matches.append({"pair_id": pair_id, "wos_index": int(w_idx), "scp_index": int(s_idx), **m})

    all_columns = list(set(list(wos_df.columns) + list(scp_df.columns)))
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

    total_input = len(wos_df) + len(scp_df)
    duplicates = len(matches)
    stats = {
        "wos_input": int(len(wos_df)),
        "scopus_input": int(len(scp_df)),
        "total_input": int(total_input),
        "merged_count": int(len(final_df)),
        "duplicates_removed": int(duplicates),
        "dedup_rate": round(duplicates / total_input, 4) if total_input else 0.0,
        "matched_pairs": int(duplicates),
        "borderline_count": int(len(borderline_rows)),
        "lost_wos_count": int(len(wos_not_matched)),
        "lost_scopus_count": int(len(scp_not_matched)),
        "conflict_count": int(len(conflicts)),
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
