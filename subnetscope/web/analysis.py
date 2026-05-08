"""Per-subnet analysis loader.

Reads markdown files from `analyses/sn<N>.md` (one per subnet), renders to
HTML, and serves on the detail page. Subnets without an analysis file
return None, so the template can hide the section entirely.

Updates: edit / overwrite the .md file and the next page load picks it up
(mtime-keyed cache).
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import markdown

log = logging.getLogger(__name__)

# Default analyses dir is project_root/analyses.
DEFAULT_ANALYSES_DIR = (
    Path(__file__).resolve().parent.parent.parent / "analyses"
)

_md = markdown.Markdown(extensions=[
    "extra",          # tables, fenced code, footnotes, etc.
    "sane_lists",
    "smarty",
    "codehilite",
])

# Pull the leading H1 out so the template can render it as a section title.
_H1_RX = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
# Pull a "> Analyzed: ..." blockquote line, used as a freshness footer.
_META_RX = re.compile(r"^\s*>\s*Analyzed:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class Analysis:
    netuid: int
    title: str | None
    analyzed_label: str | None
    body_html: str
    file_path: str
    file_mtime: float
    is_auto: bool = False

    @property
    def file_mtime_iso(self) -> str:
        return (datetime.fromtimestamp(self.file_mtime, tz=timezone.utc)
                .astimezone().strftime("%Y-%m-%d %H:%M %Z"))


class AnalysisStore:
    """Loads and renders per-subnet markdown analyses with mtime caching.

    Resolution order for ``get(netuid)``:
    1. ``<analyses_dir>/sn<N>.md``         — hand-curated (highest priority)
    2. ``<analyses_dir>/auto/sn<N>.md``    — auto-generated fallback
    """

    def __init__(self, analyses_dir: Path | str | None = None):
        self.dir = Path(analyses_dir or DEFAULT_ANALYSES_DIR)
        self._lock = threading.RLock()
        self._cache: dict[int, Analysis] = {}

    def _path_for(self, netuid: int) -> Path:
        """Return the highest-priority existing path, or the manual path."""
        manual = self.dir / f"sn{netuid}.md"
        if manual.is_file():
            return manual
        auto = self.dir / "auto" / f"sn{netuid}.md"
        if auto.is_file():
            return auto
        return manual  # doesn't exist — get() will return None

    def get(self, netuid: int) -> Analysis | None:
        path = self._path_for(netuid)
        if not path.is_file():
            with self._lock:
                self._cache.pop(netuid, None)
            return None

        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._cache.get(netuid)
            if cached and cached.file_mtime == mtime:
                return cached

        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            log.exception("read analysis file %s", path)
            return None

        # Extract title (first H1) and analyzed-on label.
        m_title = _H1_RX.search(raw)
        title = m_title.group(1).strip() if m_title else None
        if m_title:
            raw = raw[:m_title.start()] + raw[m_title.end():]

        m_meta = _META_RX.search(raw)
        analyzed_label = m_meta.group(1).strip() if m_meta else None
        if m_meta:
            raw = raw[:m_meta.start()] + raw[m_meta.end():]

        _md.reset()
        body_html = _md.convert(raw)

        analysis = Analysis(
            netuid=netuid,
            title=title,
            analyzed_label=analyzed_label,
            body_html=body_html,
            file_path=str(path),
            file_mtime=mtime,
            is_auto=("auto" + "/") in str(path) or ("auto" + "\\") in str(path),
        )
        with self._lock:
            self._cache[netuid] = analysis
        return analysis

    def list_netuids(self) -> list[int]:
        """Return sorted list of netuids that have any analysis file
        (hand-curated or auto-generated)."""
        if not self.dir.is_dir():
            return []
        seen: set[int] = set()
        for p in self.dir.glob("sn*.md"):
            stem = p.stem
            if stem.startswith("sn") and stem[2:].isdigit():
                seen.add(int(stem[2:]))
        # Also collect from auto/ subdirectory.
        auto_dir = self.dir / "auto"
        if auto_dir.is_dir():
            for p in auto_dir.glob("sn*.md"):
                stem = p.stem
                if stem.startswith("sn") and stem[2:].isdigit():
                    seen.add(int(stem[2:]))
        return sorted(seen)

    def list_curated_netuids(self) -> list[int]:
        """Return sorted list of hand-curated (non-auto) netuid files."""
        if not self.dir.is_dir():
            return []
        out = []
        for p in self.dir.glob("sn*.md"):
            stem = p.stem
            if stem.startswith("sn") and stem[2:].isdigit():
                out.append(int(stem[2:]))
        return sorted(out)


_store: AnalysisStore | None = None


def init_store(analyses_dir: Path | str | None = None) -> AnalysisStore:
    global _store
    _store = AnalysisStore(analyses_dir)
    return _store


def get_store() -> AnalysisStore:
    global _store
    if _store is None:
        _store = AnalysisStore()
    return _store
