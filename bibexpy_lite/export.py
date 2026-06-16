"""Export a merged DataFrame to a Web of Science tagged .txt file.

The .txt is consumable by VOSviewer and bibliometrix / biblioshiny, which is
the usual reason for merging WoS + Scopus. Adapted from the BibexPy core
(xlsx2vos) to take a DataFrame directly instead of round-tripping through Excel.
"""

from __future__ import annotations

import pandas as pd

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

            cr = str(values["CR"][i] or "")
            cr_list = [r.strip() for r in cr.split(";") if r.strip()]
            if cr_list:
                f.write(f"CR {cr_list[0]}\n")
                for r in cr_list[1:]:
                    f.write(f"   {r}\n")
            else:
                f.write("CR \n")

            for tag in ("NR", "TC", "Z9", "U1", "U2", "PU", "PI", "PA", "SN",
                        "EI", "J9", "JI", "PD", "PY", "VL", "AR", "DI", "EA",
                        "PG", "WC", "WE", "SC", "GA", "UT", "DA"):
                f.write(f"{tag} {values[tag][i]}\n")

            f.write("ER\n\n")
        f.write("EF\n")
