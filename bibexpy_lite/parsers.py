"""Raw WoS / Scopus parsers -> bibliographic DataFrame with standard 2-letter tags.

Vendored from the BibexPy core (wos2xlsx / scp2xlsx). Reads:
  • Web of Science plain-text export(s) (.txt, ISI tagged format)
  • Scopus CSV export(s) (.csv)
and returns a DataFrame whose columns use the standard tags (TI, DI, AU, PY,
SO, VL, BP, ...) that smart_merge() expects.
"""

from __future__ import annotations

import re
from typing import List, Union

import pandas as pd


# ════════════════════════════════════════════════════════════════════════
#  Web of Science (ISI tagged .txt)
# ════════════════════════════════════════════════════════════════════════

def _remove_strange_char(text: str) -> str:
    return re.sub(r"[^\x00-\x7F]+", "", text)


def _safe_str_replace(x, old, new):
    if pd.isna(x):
        return x
    try:
        return str(x).replace(old, new)
    except Exception:
        return x


def isi2df(lines: List[str]) -> pd.DataFrame:
    """Parse Web of Science ISI tagged lines into a DataFrame."""
    D = [line for line in lines if len(line.strip()) > 1]
    D = [_remove_strange_char(line) for line in D]
    D = [line for line in D if not line.startswith(("FN ", "VR "))]

    for i in range(1, len(D)):
        if D[i].startswith("   "):
            D[i] = D[i - 1][:3] + D[i][3:]

    papers = [i for i, line in enumerate(D) if line.startswith("PT ")]
    if not papers:
        raise ValueError("No valid ISI (Web of Science) format found in file.")

    row_papers = []
    for i in range(len(papers) - 1):
        row_papers.append(papers[i + 1] - papers[i])
    row_papers.append(len(D) - papers[-1])

    num_papers = []
    for i, count in enumerate(row_papers):
        num_papers.extend([i + 1] * count)

    df = pd.DataFrame({
        "Tag": [line[:3].strip() for line in D],
        "content": [line[3:].strip() for line in D],
        "Paper": num_papers,
    })
    df = df.groupby(["Paper", "Tag"])["content"].apply("---".join).reset_index()
    df = df.pivot(index="Paper", columns="Tag", values="content").reset_index()

    comma_tags = ["AU", "AF", "CR"]
    for tag in comma_tags:
        if tag in df.columns:
            df[tag] = df[tag].apply(lambda x: _safe_str_replace(x, "---", ";"))
    other_tags = [c for c in df.columns if c not in comma_tags]
    for tag in other_tags:
        if tag in df.columns:
            df[tag] = df[tag].apply(lambda x: _safe_str_replace(x, "---", " ").strip() if pd.notnull(x) else x)

    if "C1" in df.columns:
        df["C1raw"] = df["C1"].copy()
        df["C1"] = df["C1"].apply(lambda x: re.sub(r"\[.*?\]", "", str(x)) if pd.notnull(x) else x)
        df["C1"] = df["C1"].apply(lambda x: _safe_str_replace(x, ".", ".;") if pd.notnull(x) else x)

    df["DB"] = "ISI"
    if "AU" in df.columns:
        df["AU"] = df["AU"].apply(lambda x: _safe_str_replace(x, ",", " ").strip() if pd.notnull(x) else x)

    di_col = df["DI"].copy() if "DI" in df.columns else None
    for col in df.columns:
        if df[col].dtype == "object":
            df[col] = df[col].apply(lambda x: str(x).upper() if pd.notnull(x) else x)
    if di_col is not None:
        df["DI"] = di_col

    df = df.drop("Paper", axis=1)
    return df


def read_wos(paths: Union[str, List[str]]) -> pd.DataFrame:
    """Read one or more WoS .txt exports and concatenate them."""
    if isinstance(paths, str):
        paths = [paths]
    frames = []
    for p in paths:
        with open(p, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        if not lines:
            continue
        frames.append(isi2df(lines))
    if not frames:
        raise ValueError("No Web of Science records could be read.")
    return pd.concat(frames, ignore_index=True)


# ════════════════════════════════════════════════════════════════════════
#  Scopus (CSV)
# ════════════════════════════════════════════════════════════════════════

_SCOPUS_TAGS = [
    ("Abbreviated Source Title", "JI"), ("Affiliations", "C1"), ("Authors", "AU"),
    ("Author Names", "AU"), ("Author full names", "AF"), ("Source title", "SO"),
    ("Titles", "TI"), ("Title", "TI"), ("Publication Year", "PY"), ("Year", "PY"),
    ("Volume", "VL"), ("Issue", "IS"), ("Page count", "PP"), ("Cited by", "TC"),
    ("DOI", "DI"), ("Link", "URL"), ("Abstract", "AB"), ("Author Keywords", "DE"),
    ("Indexed Keywords", "ID"), ("Index Keywords", "ID"), ("Funding Details", "FU"),
    ("Funding Texts", "FX"), ("Funding Text 1", "FX"), ("References", "CR"),
    ("Correspondence Address", "RP"), ("Publisher", "PU"), ("Open Access", "OA"),
    ("Language of Original Document", "LA"), ("Document Type", "DT"),
    ("Source", "DB"), ("EID", "UT"),
]


def _abbrev_title(title: str) -> str:
    return title.replace(".", "").upper()


def _labelling(data: pd.DataFrame) -> pd.DataFrame:
    df_tag = pd.DataFrame(_SCOPUS_TAGS, columns=["orig", "tag"])
    label = pd.DataFrame({"orig": data.columns})
    label = label.merge(df_tag, on="orig", how="left")
    label["tag"] = label["tag"].fillna(label["orig"])
    data.columns = label["tag"]
    return data


def csvScopus2df(files: Union[str, List[str]]) -> pd.DataFrame:
    """Read Scopus CSV export(s) into a tagged DataFrame."""
    if isinstance(files, str):
        files = [files]
    all_data = []
    for i, file in enumerate(files):
        df = pd.read_csv(file, dtype=str, na_values="", keep_default_na=False, encoding="utf-8")
        df = df.fillna("")
        if i > 0 and all_data:
            common_cols = list(set(df.columns) & set(all_data[0].columns))
            all_data.append(df[common_cols])
        else:
            all_data.append(df)
    if not all_data:
        raise ValueError("No Scopus files could be read.")
    DATA = pd.concat(all_data, ignore_index=True)
    DATA = _labelling(DATA)

    if "AU" in DATA.columns:
        DATA["AU"] = DATA["AU"].str.replace(".", "", regex=False)
        DATA["AU"] = DATA["AU"].str.replace(",", ";", regex=False)
    if "C1" not in DATA.columns:
        DATA["C1"] = ""
    if "JI" in DATA.columns:
        DATA["J9"] = DATA["JI"].str.replace(".", "", regex=False)
    elif "SO" in DATA.columns:
        DATA["J9"] = DATA["SO"].apply(_abbrev_title)
        DATA["JI"] = DATA["J9"]

    di_values = DATA.get("DI", None)
    url_values = DATA.get("URL", None)
    for col in DATA.columns:
        if DATA[col].dtype == "object":
            DATA[col] = DATA[col].str.upper()
    if di_values is not None:
        DATA["DI"] = di_values
    if url_values is not None:
        DATA["URL"] = url_values
    return DATA


def read_scopus(paths: Union[str, List[str]]) -> pd.DataFrame:
    """Read one or more Scopus .csv exports and concatenate them."""
    return csvScopus2df(paths)
