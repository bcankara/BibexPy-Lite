#!/usr/bin/env python3
"""BibexPy-Lite — interactive WoS + Scopus merge (Smart Merge, DOI-based dedup).

v1-style terminal workflow, merge-only (no enrichment, no API):

    python merge.py

Project layout (same idea as BibexPy v1):

    Workspace/
      <Your Project>/
        Data/
          *.txt   (Web of Science plain-text exports — one or more)
          *.csv   (Scopus CSV exports — one or more)

You pick a project from a numbered menu; the tool reads every WoS/Scopus file in
its Data/ folder, runs Smart Merge, and writes results to a timestamped
Analysis_<...> folder inside the project.
"""

from __future__ import annotations

import glob
import os
import sys
from datetime import datetime

import pandas as pd

from bibexpy_lite import read_scopus, read_wos, smart_merge, write_vosviewer

try:
    from colorama import Fore, Style, init as _color_init
    _color_init()
except Exception:  # colorama optional
    class _Dummy:
        def __getattr__(self, _):
            return ""
    Fore = Style = _Dummy()  # type: ignore

WORKSPACE = "Workspace"


def _info(msg: str) -> None:
    print(f"{Fore.CYAN}{msg}{Style.RESET_ALL}")


def _ok(msg: str) -> None:
    print(f"{Fore.GREEN}{msg}{Style.RESET_ALL}")


def _warn(msg: str) -> None:
    print(f"{Fore.YELLOW}{msg}{Style.RESET_ALL}")


def _find_inputs(data_dir: str) -> tuple[list[str], list[str]]:
    """Return (wos_txt_files, scopus_csv_files) inside a project's Data/ folder."""
    wos = sorted(glob.glob(os.path.join(data_dir, "*.txt")))
    scp = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    return wos, scp


def run_project(project_dir: str) -> str:
    """Run Smart Merge for a single project folder. Returns the analysis dir.

    Non-interactive: reads Data/, merges, writes outputs. Used by the menu and
    importable for scripts / notebooks.
    """
    data_dir = os.path.join(project_dir, "Data")
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"No Data/ folder in {project_dir}")

    wos_files, scp_files = _find_inputs(data_dir)
    if not wos_files and not scp_files:
        raise FileNotFoundError(f"No .txt (WoS) or .csv (Scopus) files in {data_dir}")

    _info("\nReading input files...")
    if wos_files:
        print(f"  WoS:    {len(wos_files)} file(s) -> {', '.join(os.path.basename(f) for f in wos_files)}")
        wos_df = read_wos(wos_files)
    else:
        _warn("  WoS:    none found (.txt) — using empty set")
        wos_df = pd.DataFrame()
    if scp_files:
        print(f"  Scopus: {len(scp_files)} file(s) -> {', '.join(os.path.basename(f) for f in scp_files)}")
        scp_df = read_scopus(scp_files)
    else:
        _warn("  Scopus: none found (.csv) — using empty set")
        scp_df = pd.DataFrame()

    _info(f"\nLoaded: {len(wos_df)} WoS + {len(scp_df)} Scopus records")
    _info("Running Smart Merge (DOI-determinative dedup)...")
    res = smart_merge(wos_df, scp_df)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(project_dir, f"Analysis_{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    merged_xlsx = os.path.join(out_dir, "Merged.xlsx")
    merged_txt = os.path.join(out_dir, "Merged_Vos.txt")
    res.merged.to_excel(merged_xlsx, index=False)
    write_vosviewer(res.merged, merged_txt)

    if not res.borderline.empty:
        res.borderline.to_excel(os.path.join(out_dir, "Borderline_Uncertain.xlsx"), index=False)
    if not res.conflicts.empty:
        res.conflicts.to_excel(os.path.join(out_dir, "Conflict_Log.xlsx"), index=False)
    if not res.lost_wos.empty:
        res.lost_wos.to_excel(os.path.join(out_dir, "Lost_WoS.xlsx"), index=False)
    if not res.lost_scopus.empty:
        res.lost_scopus.to_excel(os.path.join(out_dir, "Lost_Scopus.xlsx"), index=False)
    pd.DataFrame([res.stats]).to_excel(os.path.join(out_dir, "Statistics.xlsx"), index=False)

    s = res.stats
    _ok("\n" + "=" * 56)
    _ok("Smart Merge complete")
    _ok("=" * 56)
    print(f"  WoS input         : {s['wos_input']}")
    print(f"  Scopus input      : {s['scopus_input']}")
    print(f"  Total input       : {s['total_input']}")
    print(f"  Duplicates removed: {s['duplicates_removed']} ({s['dedup_rate'] * 100:.1f}%)")
    print(f"  Unique records    : {s['merged_count']}")
    print(f"  Borderline (kept separate, review): {s['borderline_count']}")
    print(f"  Lost (WoS / Scopus): {s['lost_wos_count']} / {s['lost_scopus_count']}")
    if res.match_stages:
        print("  Match stages:")
        for label, n in sorted(res.match_stages.items(), key=lambda x: -x[1]):
            print(f"    - {label}: {n}")
    _ok(f"\nOutput folder: {out_dir}")
    print("  - Merged.xlsx           (deduplicated dataset)")
    print("  - Merged_Vos.txt        (VOSviewer / biblioshiny)")
    if s["borderline_count"]:
        print("  - Borderline_Uncertain.xlsx (uncertain pairs kept separate)")
    print("  - Statistics.xlsx")
    return out_dir


def main() -> int:
    print(f"{Fore.CYAN}{Style.BRIGHT}BibexPy-Lite — Smart Merge{Style.RESET_ALL}")
    print("-" * 56)

    if not os.path.isdir(WORKSPACE):
        _warn(f"'{WORKSPACE}/' folder not found. Create it and add a project:")
        print(f"  {WORKSPACE}/My Project/Data/  (put your .txt and .csv exports there)")
        return 1

    projects = sorted(
        d for d in os.listdir(WORKSPACE) if os.path.isdir(os.path.join(WORKSPACE, d))
    )
    if not projects:
        _warn(f"No project folders inside '{WORKSPACE}/'. Add one with a Data/ subfolder.")
        return 1

    print("\nProjects:")
    for i, name in enumerate(projects, 1):
        print(f"  {i}. {name}")

    while True:
        try:
            choice = int(input("\nSelect project number: "))
            if 1 <= choice <= len(projects):
                project_dir = os.path.join(WORKSPACE, projects[choice - 1])
                break
            sys.stderr.write("Invalid selection.\n")
        except (ValueError, EOFError):
            sys.stderr.write("Please enter a valid number.\n")
            return 1

    try:
        run_project(project_dir)
    except Exception as exc:
        sys.stderr.write(f"\nError: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
