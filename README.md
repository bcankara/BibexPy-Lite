# BibexPy-Lite

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/bcankara/BibexPy-Lite/blob/main/BibexPy_Lite.ipynb)

A **lightweight, terminal / Colab** tool that merges **Web of Science + Scopus**
exports into a single deduplicated dataset using BibexPy's **Smart Merge**
algorithm (DOI-determinative deduplication).

It is the "just merge" companion to the full [BibexPy](https://github.com/bcankara/BibexPy)
app: same merge algorithm, no web UI, no API/ML enrichment — ideal for a quick
run in Google Colab or a terminal, the way BibexPy v1 worked.

> **What it does:** read raw WoS `.txt` + Scopus `.csv` → merge & deduplicate →
> write `Merged.xlsx` and a VOSviewer / biblioshiny-ready `Merged_Vos.txt`.
> **What it does NOT do:** enrichment, APIs, filtering, harmonization, reporting.
> For those, use the full BibexPy app.

---

## Smart Merge in one line

Records are matched in stages: **negative rules → DOI exact → PMID exact →
Title+Year+Surname → Journal+Volume+Pages → borderline**. **DOI is
determinative** — two records whose normalized DOIs differ are *never* the same
publication (no auto-merge, no false dedup). Field values are combined with
fixed per-field source preferences (e.g. abstract/authors from Scopus, citations
from Web of Science).

Uncertain pairs (title similarity 0.80–0.92) are **kept separate** and written
to `Borderline_Uncertain.xlsx` for you to review manually.

---

## Use in Google Colab (recommended)

Click the **Open in Colab** badge above, then run the cells. You can try the
bundled *Sample Project* or upload your own WoS/Scopus exports.

## Use in a terminal

```bash
git clone https://github.com/bcankara/BibexPy-Lite.git
cd BibexPy-Lite
pip install -r requirements.txt
python merge.py
```

### Project layout

Put your raw exports under `Workspace/<Your Project>/Data/`:

```
Workspace/
  My Project/
    Data/
      savedrecs.txt      # Web of Science plain-text export(s) — one or more
      scopus.csv         # Scopus CSV export(s) — one or more
```

Run `python merge.py`, pick your project from the numbered menu, and the
results are written to `Workspace/My Project/Analysis_<timestamp>/`:

| File | Description |
|------|-------------|
| `Merged.xlsx` | Final deduplicated dataset |
| `Merged_Vos.txt` | WoS-tagged text for VOSviewer / biblioshiny |
| `Borderline_Uncertain.xlsx` | Uncertain pairs kept separate (review) |
| `Conflict_Log.xlsx` | Field conflicts resolved during merge |
| `Lost_WoS.xlsx` / `Lost_Scopus.xlsx` | Records with no match |
| `Statistics.xlsx` | Summary counts |

## Use as a library

```python
from bibexpy_lite import read_wos, read_scopus, smart_merge

wos = read_wos("Workspace/My Project/Data/savedrecs.txt")
scp = read_scopus("Workspace/My Project/Data/scopus.csv")
res = smart_merge(wos, scp)

res.merged.to_excel("merged.xlsx", index=False)
print(res.stats)            # counts: duplicates_removed, merged_count, ...
print(res.borderline)       # uncertain pairs
```

---

## Relationship to the main BibexPy

The Smart Merge algorithm here (`bibexpy_lite/smart_merge.py`) is a **vendored
copy** of the canonical implementation in
[BibexPy](https://github.com/bcankara/BibexPy) (`apps/api/services/smart_merger.py`).
The algorithm is the single source of truth there; this repo keeps a copy so it
can run with no web dependencies. They are kept in sync and produce identical
merge results.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

If you use BibexPy in research, please cite:
Kara, Şahin & Dirsehan (2025), *SoftwareX* — https://doi.org/10.1016/j.softx.2025.102098
