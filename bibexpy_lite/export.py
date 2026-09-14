"""Export a merged DataFrame for downstream bibliometric tools.

* ``write_vosviewer`` — Web of Science tagged .txt, consumable by VOSviewer and
  bibliometrix / biblioshiny. Adapted from the BibexPy core (xlsx2vos) to take a
  DataFrame directly instead of round-tripping through Excel.
* ``write_excel`` — .xlsx that biblioshiny can import directly (carries SR).

Both mirror the main BibexPy export boundary: cited references are normalized
to WoS grammar and SR is generated for spreadsheets.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .cr_normalize import count_refs, normalize_cr

# Output tag -> source column. Most are identity; a few map to differently
# named columns produced by the parsers.
_DESIRED_COLUMNS = {
    "PT": "PT", "AU": "AU", "AF": "AF", "TI": "TI", "SO": "SO", "LA": "LA",
    "DT": "DT", "DE": "DE", "ID": "ID", "AB": "AB", "C1": "C1", "C3": "C3",
    "RP": "RP", "EM": "EM", "FU": "FU", "FX": "FX", "CR": "CR", "NR": "NR",
    "TC": "TC", "Z9": "Z9", "U1": "U1", "U2": "U2", "PU": "PU", "PI": "PI",
    "PA": "PA", "SN": "SN", "EI": "ISSN", "J9": "J9", "JI": "JI", "PD": "PD",
    "PY": "PY", "VL": "VL", "AR": "Art. No.", "DI": "DI", "EA": "EA",
    "PG": "PG", "WC": "WC", "WE": "WE", "SC": "SC", "GA": "GA", "UT": "UT",
    "DA": "DA",
}


def write_vosviewer(df: pd.DataFrame, output_txt_path: str) -> None:
    """Write `df` as a WoS-tagged .txt (VOSviewer / biblioshiny compatible)."""
    values = {tag: [] for tag in _DESIRED_COLUMNS}
    for _, row in df.iterrows():
        for tag, src in _DESIRED_COLUMNS.items():
            v = row.get(src, "")
            values[tag].append(v if pd.notna(v) else "")

    n = len(df)
    with open(output_txt_path, "w", encoding="utf-8") as f:
        f.write("FN Clarivate Analytics Web of Science\n")
        f.write("VR 1.0\n\n")
        for i in range(n):
            f.write(f"PT {values['PT'][i] or 'J'}\n")

            au = str(values["AU"][i] or "")
            au_list = [a.strip() for a in au.split(";") if a.strip()]
            if au_list:
                f.write(f"AU {au_list[0]}\n")
                for a in au_list[1:]:
                    f.write(f"   {a}\n")
            else:
                f.write("AU \n")

            af = str(values["AF"][i] or "")
            af_list = [a.strip() for a in af.split(";") if a.strip()]
            if af_list:
                f.write(f"AF {af_list[0]}\n")
                for a in af_list[1:]:
                    f.write(f"   {a}\n")
            else:
                f.write("AF \n")

            f.write(f"TI {values['TI'][i]}\n")
            f.write(f"SO {values['SO'][i]}\n")
            f.write(f"LA {values['LA'][i]}\n")
            f.write(f"DT {values['DT'][i]}\n")
            f.write(f"DE {values['DE'][i]}\n")
            f.write(f"ID {values['ID'][i]}\n")
            f.write(f"AB {values['AB'][i]}\n")

            c1 = str(values["C1"][i] or "")
            authors = [a.strip() for a in af.split(";") if a.strip()]
            addresses = [a.strip() for a in c1.split(";") if a.strip()]
            if authors and addresses:
                f.write(f"C1 [{authors[0]}] {addresses[0]}\n")
                idx_addr = 1
                for k in range(1, min(len(authors), len(addresses))):
                    f.write(f"   [{authors[k]}] {addresses[k]}\n")
                    idx_addr = k + 1
                if len(authors) > len(addresses):
                    last = addresses[-1]
                    for k in range(idx_addr, len(authors)):
                        f.write(f"   [{authors[k]}] {last}\n")
            else:
                f.write("C1 \n")

            f.write(f"C3 {values['C3'][i]}\n")
            f.write(f"RP {values['RP'][i]}\n")
            f.write(f"EM {values['EM'][i]}\n")
            f.write(f"FU {values['FU'][i]}\n")
            f.write(f"FX {values['FX'][i]}\n")

            # Normalize BEFORE splitting: in Scopus grammar ';' also separates
            # authors, so a raw split shreds each reference into author
            # fragments. After normalize_cr ';' is only a reference boundary.
            cr = normalize_cr(str(values["CR"][i] or ""))
            cr_list = [r.strip() for r in cr.split(";") if r.strip()]
            if cr_list:
                f.write(f"CR {cr_list[0]}\n")
                for r in cr_list[1:]:
                    f.write(f"   {r}\n")
            else:
                f.write("CR \n")

            # NR blank or 0 next to a non-empty CR: count the references —
            # readers use NR as the reference count; genuinely reference-less
            # records have an empty CR anyway.
            nr = values["NR"][i]
            if str(nr).strip() in ("", "nan", "NaN", "None", "0", "0.0") and cr:
                nr = count_refs(cr)
            f.write(f"NR {nr}\n")

            for tag in ("TC", "Z9", "U1", "U2", "PU", "PI", "PA", "SN",
                        "EI", "J9", "JI", "PD", "PY", "VL", "AR", "DI", "EA",
                        "PG", "WC", "WE", "SC", "GA", "UT", "DA"):
                f.write(f"{tag} {values[tag][i]}\n")

            f.write("ER\n\n")
        f.write("EF\n")


# ════════════════════════════════════════════════════════════════════════
#  SR (Short Reference) — bibliometrix / biblioshiny compatibility
# ════════════════════════════════════════════════════════════════════════
# Vendored from the main BibexPy export boundary (apps/api/services/exporter.py).

def _blank(v: Any) -> bool:
    s = str(v).strip()
    return s == "" or s.upper() in ("NAN", "NONE", "NA")


def _fmt_year(v: Any) -> str:
    """Render PY the way R's paste() would: 2020.0 -> "2020"."""
    if _blank(v):
        return "NA"
    try:
        f = float(v)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def _first_author(au: Any) -> str:
    """First ';' part of AU with commas turned into spaces (bibliometrix SR())."""
    if _blank(au):
        return "NA"
    first = str(au).split(";")[0].strip().replace(",", " ")
    first = re.sub(r"\s+", " ", first).strip()
    return first or "NA"


def _sr_source(row: pd.Series, has_j9: bool, has_ji: bool, has_so: bool) -> str:
    """Source abbreviation: J9 -> (SO when J9 and JI are both blank) -> JI with
    dots as spaces. Without a J9 column: JI, else SO — the same priority chain
    as bibliometrix SR()."""
    j9 = row.get("J9") if has_j9 else None
    ji = row.get("JI") if has_ji else None
    so = row.get("SO") if has_so else None
    if has_j9:
        if not _blank(j9):
            return str(j9).strip()
        if _blank(ji):
            return "" if _blank(so) else str(so).strip()
        return re.sub(r"\s+", " ", str(ji).replace(".", " ")).strip()
    val = ji if not _blank(ji) else so
    if _blank(val):
        return ""
    return re.sub(r"\s+", " ", str(val).replace(".", " ")).strip()


def ensure_sr(df: pd.DataFrame) -> pd.DataFrame:
    """Add SR / SR_FULL when missing (faithful to bibliometrix
    ``metaTagExtraction(Field="SR")``).

    biblioshiny does NOT run convert2df when importing an xlsx/csv: it reads the
    file raw and assumes SR already exists (``wcTable`` does an unguarded
    ``rep(M$SR, lengths(WC))`` and loading crashes with "differing number of
    rows: 0, N" without it). Format "SURNAME IN, YEAR, SOURCE"; duplicates get
    bibliometrix's ITERATIVE suffixes (three copies -> X, X-a, X-a-b).
    """
    if "SR" in df.columns and df["SR"].astype(str).str.strip().ne("").any():
        return df
    if "AU" not in df.columns:
        return df  # SR cannot be built; biblioshiny could not use the data anyway

    has_j9 = "J9" in df.columns
    has_ji = "JI" in df.columns
    has_so = "SO" in df.columns

    parts = []
    for _, row in df.iterrows():
        fa = _first_author(row.get("AU"))
        py = _fmt_year(row.get("PY")) if "PY" in df.columns else "NA"
        src = _sr_source(row, has_j9, has_ji, has_so)
        sr = f"{fa}, {py}, {src}" if src else f"{fa}, {py}"
        parts.append(re.sub(r"\s+", " ", sr).strip())

    sr_full = list(parts)
    # bibliometrix's compounding suffix loop: each round appends -a, then -b ...
    # to whatever is still duplicated.
    letters = "abcdefghijklmnopqrstuvwxyz"
    sr = list(parts)
    for i in range(len(letters)):
        seen: set = set()
        dup_idx = []
        for idx, v in enumerate(sr):
            if v in seen:
                dup_idx.append(idx)
            else:
                seen.add(v)
        if not dup_idx:
            break
        for idx in dup_idx:
            sr[idx] = f"{sr[idx]}-{letters[i]}"

    out = df.copy(deep=False)
    out["SR"] = sr
    if "SR_FULL" not in out.columns:
        out["SR_FULL"] = sr_full
    return out


def write_excel(df: pd.DataFrame, output_xlsx_path: str) -> None:
    """Write `df` as .xlsx that biblioshiny can import directly (SR included)."""
    ensure_sr(df).to_excel(output_xlsx_path, index=False)
