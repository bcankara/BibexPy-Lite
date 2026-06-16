"""BibexPy-Lite — terminal / Colab tool that merges Web of Science + Scopus
exports with the BibexPy Smart Merge algorithm (DOI-determinative dedup).

Public API:
    from bibexpy_lite import smart_merge, read_wos, read_scopus, write_vosviewer
"""

from .smart_merge import SmartMergeResult, smart_merge
from .parsers import read_scopus, read_wos
from .export import write_vosviewer

__all__ = ["smart_merge", "SmartMergeResult", "read_wos", "read_scopus", "write_vosviewer"]
__version__ = "1.0.0"
