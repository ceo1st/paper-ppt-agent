"""Project directory management for SVG-based presentation generation.

Creates and manages the project workspace structure required by the
SVG generation → post-processing → PPTX export pipeline.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable

from backend.config import CANVAS_FORMATS


DEFAULT_PROJECT_SUBDIRS = (
    "svg_output",
    "svg_final",
    "images",
    "notes",
    "templates",
    "sources",
    "exports",
)

PROVIDER_PROJECT_SUBDIRS = (
    "svg_output",
    "sources",
)

_DIR_ROLES = {
    "svg_output": "live SVG slides generated page by page",
    "svg_final": "post-processed SVGs created during finalize/export",
    "sources": "parsed source paper text and extracted paper figures",
    "provider_memory": "private paper grounding, evidence cards, and slide contexts",
    "debug": "LLM prompts/responses and intermediate research/debug traces",
    "svg_archive": "repair/refine snapshots of SVG attempts",
    "exports": "finished PPTX downloads",
    "notes": "speaker notes matched to SVG stems",
    "templates": "copied template references for Agent/template-aware runs",
    "images": "user web-image-search downloads",
}


def init_project(
    name: str,
    canvas_format: str = "ppt169",
    base_dir: Path = Path("workspaces"),
    initial_subdirs: Iterable[str] | None = None,
) -> Path:
    """Create a new project directory with required structure.

    Args:
        name: Project name.
        canvas_format: Canvas format key (e.g., "ppt169").
        base_dir: Parent directory for projects.

    Returns:
        Path to the created project directory.
    """
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    timestamp = now.strftime("%H%M%S")
    project_name = f"{name}_{canvas_format}_{date_str}_{timestamp}"
    project_dir = base_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    # Create only the directories required at job start. Other well-known
    # workspace directories are created lazily by the stage that owns them so
    # aborted/partial runs remain easy to inspect.
    subdirs = tuple(initial_subdirs) if initial_subdirs is not None else DEFAULT_PROJECT_SUBDIRS
    for subdir in subdirs:
        (project_dir / subdir).mkdir(exist_ok=True)

    _write_workspace_manifest(
        project_dir,
        name=name,
        canvas_format=canvas_format,
        created=date_str,
        initial_subdirs=subdirs,
    )

    return project_dir


def _write_workspace_manifest(
    project_dir: Path,
    *,
    name: str,
    canvas_format: str,
    created: str,
    initial_subdirs: Iterable[str],
) -> None:
    fmt = CANVAS_FORMATS.get(canvas_format, CANVAS_FORMATS["ppt169"])
    subdir_set = set(initial_subdirs)
    lines = [
        f"# {name}",
        "",
        f"- **Format**: {fmt['name']} ({fmt['ratio']})",
        f"- **ViewBox**: `{fmt['viewbox']}`",
        f"- **Created**: {created}",
        "",
        "## Key Files",
        "",
        "- `manuscript.md`: slide manuscript created by research/planning",
        "- `design_spec.md`: visual plan and per-slide layout contract",
        "- `deck_plan.md`: normalized slide matrix used by SVG generation",
        "",
        "## Directories",
        "",
    ]
    for name_key in sorted(_DIR_ROLES):
        status = "created at start" if name_key in subdir_set else "created on demand"
        lines.append(f"- `{name_key}/`: {_DIR_ROLES[name_key]} ({status})")
    (project_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def clone_project_for_refine(
    source_project_dir: Path,
    refine_job_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Clone a completed project into an isolated workspace for refine.

    The cloned workspace keeps the source assets, manuscript, design spec,
    and current SVGs, but starts with a clean ``exports/`` and ``svg_archive/``
    so parallel refine jobs cannot overwrite each other's outputs.
    """
    source_project_dir = Path(source_project_dir)
    if not source_project_dir.exists():
        raise FileNotFoundError(
            f"Source project directory does not exist: {source_project_dir}"
        )

    target_base = base_dir or source_project_dir.parent
    short_id = refine_job_id[:8]
    target_dir = target_base / f"{source_project_dir.name}_refine_{short_id}"

    # If a previous refine job re-used the same short id (unlikely but
    # possible), disambiguate rather than crash.
    suffix = 2
    while target_dir.exists():
        target_dir = target_base / f"{source_project_dir.name}_refine_{short_id}_{suffix}"
        suffix += 1

    shutil.copytree(
        source_project_dir,
        target_dir,
        ignore=shutil.ignore_patterns("exports", "svg_archive", "__pycache__", ".__*"),
    )
    (target_dir / "exports").mkdir(parents=True, exist_ok=True)
    (target_dir / "svg_archive").mkdir(parents=True, exist_ok=True)
    return target_dir


def cleanup_project_dir(project_dir: Path | str | None) -> bool:
    """Best-effort delete of an abandoned project directory.

    Used when a job is cancelled or fails before producing any deliverable
    so the workspace doesn't accumulate orphaned partial runs. Returns
    ``True`` if the directory was deleted, ``False`` otherwise (missing,
    permission denied, etc.). Never raises.
    """
    if not project_dir:
        return False
    path = Path(project_dir)
    if not path.exists() or not path.is_dir():
        return False
    try:
        shutil.rmtree(path, ignore_errors=False)
        return True
    except OSError:
        # Fall back to ignore_errors so we never block the cancel path
        # on a stuck file handle (Windows AV scanners, open SVG previews,
        # etc.).
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()


def has_deliverable(project_dir: Path | str | None) -> bool:
    """True if *project_dir* contains at least one exported PPTX file.

    Cancel paths use this to decide whether to keep partial work or scrub
    the workspace.
    """
    if not project_dir:
        return False
    path = Path(project_dir)
    exports = path / "exports"
    if not exports.exists():
        return False
    try:
        return any(exports.glob("*.pptx"))
    except OSError:
        return False


def prepare_for_finalize(project_dir: Path) -> None:
    """Copy svg_output/ to svg_final/ in preparation for post-processing.

    Imported templates can reference sibling assets such as
    ``assets/logo.png`` from the generated SVG.  Keep those resources next to
    the copied SVGs so the finalization image-embedding pass can resolve them.
    """
    svg_output = project_dir / "svg_output"
    svg_final = project_dir / "svg_final"

    if svg_final.exists():
        shutil.rmtree(svg_final)
    svg_final.mkdir()

    for svg_file in get_svg_files(project_dir, "output"):
        shutil.copy2(svg_file, svg_final / svg_file.name)

    sync_svg_output_assets_to_final(project_dir)


def sync_svg_output_assets_to_final(project_dir: Path) -> None:
    """Copy non-SVG resource directories from svg_output/ to svg_final/.

    This is intentionally separate from :func:`prepare_for_finalize` so older
    workspaces can be repaired during re-export without overwriting edited
    ``svg_final/*.svg`` files.
    """
    svg_output = project_dir / "svg_output"
    svg_final = project_dir / "svg_final"
    if not svg_output.exists() or not svg_final.exists():
        return

    for child in sorted(svg_output.iterdir(), key=lambda path: path.name):
        if child.is_file() or child.suffix.lower() == ".svg":
            continue
        if child.name.startswith(".__"):
            continue
        target = svg_final / child.name
        if target.exists():
            shutil.rmtree(target)
        if child.is_dir():
            shutil.copytree(
                child,
                target,
                ignore=shutil.ignore_patterns(".__*", "__pycache__"),
            )


def get_svg_files(project_dir: Path, source: str = "final") -> list[Path]:
    """Get sorted list of SVG files from a project directory.

    Args:
        project_dir: Project directory path.
        source: "output" or "final".

    Returns:
        Sorted list of SVG file paths.
    """
    svg_dir = project_dir / f"svg_{source}"
    if not svg_dir.exists():
        return []
    candidates = sorted(
        path
        for path in svg_dir.glob("*.svg")
        if not path.name.startswith(".__render_") and not path.name.startswith(".__preview_")
    )
    indexed: dict[int, Path] = {}
    unindexed: list[Path] = []
    for path in candidates:
        index = slide_index_from_svg_path(path)
        if index is None or index < 1:
            unindexed.append(path)
            continue
        current = indexed.get(index)
        if current is None or _svg_candidate_rank(path) > _svg_candidate_rank(current):
            indexed[index] = path
    return [indexed[index] for index in sorted(indexed)] + unindexed


def _svg_candidate_rank(path: Path) -> tuple[int, int, str]:
    """Prefer the newest file when multiple names represent one slide."""
    try:
        modified = path.stat().st_mtime_ns
    except OSError:
        modified = 0
    canonical = int(bool(re.fullmatch(r"slide[_-]\d+", path.stem, re.IGNORECASE)))
    return modified, canonical, path.name


_SLIDE_INDEX_PATTERNS = (
    re.compile(r"^0*(\d+)(?:[_-]|$)", re.IGNORECASE),
    re.compile(r"^slide[_-]0*(\d+)(?:[_-]|$)", re.IGNORECASE),
)


def slide_index_from_svg_path(svg_path: Path, fallback_index: int | None = None) -> int | None:
    """Return the 1-based slide index encoded in common SVG filenames."""
    stem = Path(svg_path).stem
    for pattern in _SLIDE_INDEX_PATTERNS:
        match = pattern.match(stem)
        if not match:
            continue
        try:
            return int(match.group(1))
        except ValueError:
            break
    return fallback_index


def get_notes(project_dir: Path, svg_files: list[Path]) -> dict[str, str]:
    """Match speaker notes files to SVG files.

    Returns:
        Dict mapping SVG stem to notes markdown content.
    """
    notes_dir = project_dir / "notes"
    if not notes_dir.exists():
        return {}

    notes = {}
    for svg_path in svg_files:
        stem = svg_path.stem
        # Try exact match first
        md_path = notes_dir / f"{stem}.md"
        if md_path.exists():
            notes[stem] = md_path.read_text(encoding="utf-8")
    return notes
