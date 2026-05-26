#!/usr/bin/env python
"""Build a paper asset pack for Paper PPT Agent mode.

The script is intentionally self-contained so Claude Code/Codex can run it
inside the generated workspace without importing backend modules. It extracts
readable paper text plus figure records that include page/location/caption
context and SVG-ready image hrefs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".svg"}
RASTER_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
UNSUPPORTED_EXTENSIONS = {".eps", ".ps"}
MIN_IMAGE_SIDE = 80
PDF_REGION_DPI = 200
PDF_IMAGE_DPI = 160

CAPTION_RE = re.compile(
    r"^\s*((?:fig(?:ure)?|table)\s*\.?\s*\d+[a-z]?|[图表]\s*\d+[a-z]?)\b"
    r"[:.\s-]*(.*)",
    re.IGNORECASE,
)


@dataclass
class FigureRecord:
    id: str
    href: str
    path: str
    caption: str = ""
    label: str | None = None
    source: str = ""
    page_number: int | None = None
    bbox: list[float] | None = None
    extraction_method: str = "unknown"
    available: bool = True
    natural_width: int = 0
    natural_height: int = 0
    aspect_ratio: float = 0.0
    context_before: str = ""
    context_after: str = ""
    section_hint: str = ""
    notes: list[str] | None = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="agent_task.json")
    parser.add_argument("--source")
    parser.add_argument("--source-type", choices=["pdf", "latex"])
    parser.add_argument("--out", default="source_assets")
    args = parser.parse_args()

    workspace = Path.cwd()
    task = _read_json(workspace / args.task)
    source_info = task.get("source") if isinstance(task.get("source"), dict) else {}
    source_type = args.source_type or str(source_info.get("type") or "").lower()
    source_path = Path(args.source or source_info.get("path") or "")
    if not source_path.is_absolute():
        source_path = (workspace / source_path).resolve()

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = (workspace / out_dir).resolve()
    images_dir = out_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    if source_type == "pdf" or source_path.suffix.lower() == ".pdf":
        result = extract_pdf(source_path, out_dir, images_dir)
    else:
        result = extract_latex_source(source_path, out_dir, images_dir)

    (out_dir / "figures.json").write_text(
        json.dumps([asdict(item) for item in result["figures"]], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "paper.md").write_text(result["paper_markdown"], encoding="utf-8")
    (out_dir / "figures.md").write_text(
        _format_figures_markdown(result["figures"]), encoding="utf-8"
    )
    (out_dir / "README.md").write_text(_asset_readme(), encoding="utf-8")
    summary = {
        "source": str(source_path),
        "source_type": source_type,
        "paper_markdown": "source_assets/paper.md",
        "figures_json": "source_assets/figures.json",
        "figures_markdown": "source_assets/figures.md",
        "figures": len(result["figures"]),
        "available_figures": sum(1 for item in result["figures"] if item.available),
    }
    (out_dir / "asset_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def extract_pdf(source_path: Path, out_dir: Path, images_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - environment issue
        raise RuntimeError("PyMuPDF is required for PDF asset extraction") from exc

    doc = fitz.open(str(source_path))
    if doc.needs_pass:
        raise RuntimeError(f"PDF appears password protected: {source_path}")

    figures: list[FigureRecord] = []
    paper_lines: list[str] = [f"# {_guess_pdf_title(doc, source_path)}", ""]
    seen_hashes: set[str] = set()

    for page_index, page in enumerate(doc):
        page_no = page_index + 1
        page_text = page.get_text("text") or ""
        paper_lines.append(f"## Page {page_no}")
        paper_lines.append(page_text.strip())
        paper_lines.append("")

        text_blocks = _pdf_text_blocks(page)
        captions = _pdf_caption_blocks(text_blocks)
        page_context = _context_lines(page_text)
        section_hint = _guess_section_from_page_text(page_text)
        page_rect = page.rect

        for image_info in _pdf_image_infos(page):
            bbox = fitz.Rect(image_info.get("bbox", (0, 0, 0, 0)))
            xref = int(image_info.get("xref") or 0)
            if xref <= 0 or not _pdf_meaningful_bbox(page_rect, bbox, image_info):
                continue
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.width < MIN_IMAGE_SIDE or pix.height < MIN_IMAGE_SIDE:
                    continue
                if pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_bytes = pix.tobytes("png")
            except Exception:
                continue
            digest = hashlib.md5(img_bytes).hexdigest()[:12]
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            caption = _nearest_caption_text(bbox, captions)
            name = f"pdf_fig_{len(figures) + 1:03d}_p{page_no}_{digest}.png"
            target = images_dir / name
            target.write_bytes(img_bytes)
            figures.append(
                FigureRecord(
                    id=target.stem,
                    href=f"../source_assets/images/{target.name}",
                    path=_posix(target),
                    caption=caption,
                    source="pdf",
                    page_number=page_no,
                    bbox=_rect_list(bbox),
                    extraction_method="embedded_image",
                    natural_width=int(pix.width),
                    natural_height=int(pix.height),
                    aspect_ratio=_ratio(int(pix.width), int(pix.height)),
                    context_before=page_context[0],
                    context_after=page_context[1],
                    section_hint=section_hint,
                )
            )

        used_caption_keys = {_caption_key(fig.caption) for fig in figures if fig.page_number == page_no}
        for cap in captions:
            caption = cap["text"]
            if _caption_key(caption) in used_caption_keys:
                continue
            clip = _caption_region_clip(page_rect, cap["rect"], text_blocks)
            if clip is None:
                continue
            try:
                mat = fitz.Matrix(PDF_REGION_DPI / 72, PDF_REGION_DPI / 72)
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                if pix.width < MIN_IMAGE_SIDE or pix.height < MIN_IMAGE_SIDE:
                    continue
                img_bytes = pix.tobytes("png")
            except Exception:
                continue
            digest = hashlib.md5(img_bytes).hexdigest()[:12]
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            name = f"pdf_fig_{len(figures) + 1:03d}_p{page_no}_{digest}.png"
            target = images_dir / name
            target.write_bytes(img_bytes)
            figures.append(
                FigureRecord(
                    id=target.stem,
                    href=f"../source_assets/images/{target.name}",
                    path=_posix(target),
                    caption=caption,
                    source="pdf",
                    page_number=page_no,
                    bbox=_rect_list(clip),
                    extraction_method="caption_region",
                    natural_width=int(pix.width),
                    natural_height=int(pix.height),
                    aspect_ratio=_ratio(int(pix.width), int(pix.height)),
                    context_before=page_context[0],
                    context_after=page_context[1],
                    section_hint=section_hint,
                )
            )

    doc.close()
    _append_figure_tokens(paper_lines, figures)
    return {"paper_markdown": "\n".join(paper_lines).strip() + "\n", "figures": figures}


def extract_latex_source(source_path: Path, out_dir: Path, images_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    source_root = source_path
    if source_path.is_file() and _is_archive(source_path):
        source_root = out_dir / "latex_src"
        if source_root.exists():
            shutil.rmtree(source_root)
        source_root.mkdir(parents=True, exist_ok=True)
        _extract_archive(source_path, source_root)

    main_tex = _find_main_tex(source_root if source_root.is_dir() else source_path.parent)
    if source_path.is_file() and source_path.suffix.lower() == ".tex":
        main_tex = source_path
    if main_tex is None:
        raise RuntimeError(f"No TeX file found in {source_path}")

    root_dir = main_tex.parent
    content = _resolve_tex_includes(main_tex, root_dir)
    title = _clean_latex(_extract_command_argument(content, "title")) or main_tex.stem
    abstract = _clean_latex(_extract_environment(content, "abstract"))
    graphics_dirs = _graphicspath_dirs(content, root_dir)
    figures = _latex_figures(content, root_dir, graphics_dirs, images_dir)

    markdown = [f"# {title}", ""]
    if abstract:
        markdown.extend(["## Abstract", "", abstract, ""])
    markdown.append(_basic_latex_to_markdown(content))
    markdown.append("")
    _append_figure_tokens(markdown, figures)
    return {"paper_markdown": "\n".join(markdown).strip() + "\n", "figures": figures}


def _latex_figures(
    content: str,
    root_dir: Path,
    graphics_dirs: list[Path],
    images_dir: Path,
) -> list[FigureRecord]:
    figures: list[FigureRecord] = []
    figure_envs = list(
        re.finditer(r"\\begin\{figure\*?\}(.+?)\\end\{figure\*?\}", content, re.DOTALL)
    )
    if not figure_envs:
        figure_envs = list(re.finditer(r"(\\includegraphics(?:\[[^\]]*\])?\{[^}]+\})", content))

    for env_index, match in enumerate(figure_envs, 1):
        body = match.group(1)
        caption = _clean_latex(_extract_command_argument(body, "caption"))
        label = _extract_command_argument(body, "label") or None
        paths = re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", body)
        for path_index, raw_path in enumerate(paths, 1):
            source = _resolve_latex_image(raw_path.strip(), root_dir, graphics_dirs)
            notes: list[str] = []
            available = source is not None and source.exists()
            target = images_dir / f"tex_fig_{len(figures) + 1:03d}_{Path(raw_path).stem or 'figure'}.png"
            natural_w = natural_h = 0
            href = ""
            path_text = str(raw_path)
            method = "latex_includegraphics"

            if available and source is not None:
                suffix = source.suffix.lower()
                try:
                    if suffix in RASTER_EXTENSIONS or suffix == ".svg":
                        target = images_dir / f"tex_fig_{len(figures) + 1:03d}_{source.name}"
                        shutil.copy2(source, target)
                    elif suffix == ".pdf":
                        target = images_dir / f"tex_fig_{len(figures) + 1:03d}_{source.stem}.png"
                        _render_pdf_first_page(source, target)
                        method = "latex_pdf_graphic_rendered"
                    elif suffix in UNSUPPORTED_EXTENSIONS:
                        available = False
                        notes.append(f"{suffix} graphics cannot be embedded directly; convert manually if needed.")
                        target = source
                    else:
                        target = images_dir / f"tex_fig_{len(figures) + 1:03d}_{source.name}"
                        shutil.copy2(source, target)
                    if available:
                        natural_w, natural_h = _probe_image_size(target)
                        href = f"../source_assets/images/{target.name}"
                        path_text = _posix(target)
                except Exception as exc:
                    available = False
                    notes.append(f"Failed to prepare image: {exc}")
                    target = source
                    path_text = _posix(source)
            else:
                notes.append("Image file referenced by TeX was not found in the uploaded source.")

            fig_id_base = target.stem if available else f"tex_fig_{env_index:03d}_{path_index:02d}"
            figures.append(
                FigureRecord(
                    id=fig_id_base,
                    href=href,
                    path=path_text,
                    caption=caption,
                    label=label,
                    source="latex",
                    extraction_method=method,
                    available=available,
                    natural_width=natural_w,
                    natural_height=natural_h,
                    aspect_ratio=_ratio(natural_w, natural_h),
                    notes=notes,
                )
            )
    return figures


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_archive(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() == ".zip" or name.endswith((".tar.gz", ".tgz", ".tar"))


def _extract_archive(archive_path: Path, target_dir: Path) -> None:
    if archive_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = _safe_target(target_dir, info.filename)
                if target is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return
    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            target = _safe_target(target_dir, member.name)
            if target is None:
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _safe_target(root: Path, raw_name: str) -> Path | None:
    try:
        target = (root / raw_name).resolve()
        target.relative_to(root.resolve())
        return target
    except Exception:
        return None


def _find_main_tex(root: Path) -> Path | None:
    candidates = list(root.rglob("*.tex")) if root.is_dir() else [root]
    for tex in candidates:
        try:
            text = tex.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if r"\documentclass" in text or r"\begin{document}" in text:
            return tex
    return candidates[0] if candidates else None


def _resolve_tex_includes(
    tex_path: Path,
    root_dir: Path,
    depth: int = 0,
    visited: set[Path] | None = None,
) -> str:
    if depth > 20:
        return ""
    visited = visited or set()
    resolved = tex_path.resolve()
    if resolved in visited:
        return ""
    visited.add(resolved)
    try:
        text = tex_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    text = _strip_latex_comments(text)

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if not raw or raw.startswith("\\"):
            return match.group(0)
        name = raw if raw.endswith(".tex") else f"{raw}.tex"
        for parent in (tex_path.parent, root_dir):
            candidate = (parent / name).resolve()
            try:
                candidate.relative_to(root_dir.resolve())
            except ValueError:
                continue
            if candidate.exists():
                return _resolve_tex_includes(candidate, root_dir, depth + 1, visited)
        return match.group(0)

    return re.sub(r"\\(?:input|include)\s*\{([^}]+)\}", replace, text)


def _strip_latex_comments(text: str) -> str:
    lines = []
    for line in text.splitlines():
        cut = None
        for idx, char in enumerate(line):
            if char != "%":
                continue
            slash_count = 0
            cursor = idx - 1
            while cursor >= 0 and line[cursor] == "\\":
                slash_count += 1
                cursor -= 1
            if slash_count % 2 == 0:
                cut = idx
                break
        lines.append(line[:cut] if cut is not None else line)
    return "\n".join(lines)


def _extract_command_argument(text: str, command: str) -> str:
    match = re.search(rf"\\{re.escape(command)}\*?\s*(?:\[[^\]]*\])?\s*\{{", text)
    if not match:
        return ""
    return _read_braced(text, match.end() - 1)


def _extract_environment(text: str, name: str) -> str:
    match = re.search(
        rf"\\begin\{{{re.escape(name)}\}}(.+?)\\end\{{{re.escape(name)}\}}",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _read_braced(text: str, open_index: int) -> str:
    if open_index >= len(text) or text[open_index] != "{":
        return ""
    depth = 0
    start = open_index + 1
    for idx in range(open_index, len(text)):
        char = text[idx]
        if idx > 0 and text[idx - 1] == "\\":
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx]
    return ""


def _graphicspath_dirs(content: str, root_dir: Path) -> list[Path]:
    dirs = [root_dir]
    match = re.search(r"\\graphicspath\s*\{(.+?)\}", content, re.DOTALL)
    if not match:
        return dirs
    for item in re.findall(r"\{([^{}]+)\}", match.group(1)):
        candidate = (root_dir / item).resolve()
        if candidate.exists():
            dirs.append(candidate)
    return dirs


def _resolve_latex_image(raw: str, root_dir: Path, graphics_dirs: list[Path]) -> Path | None:
    raw_path = Path(raw)
    candidates: list[Path] = []
    for parent in graphics_dirs:
        candidates.append(parent / raw_path)
        if raw_path.suffix:
            continue
        for ext in [".png", ".jpg", ".jpeg", ".pdf", ".svg", ".webp", ".eps"]:
            candidates.append(parent / f"{raw}{ext}")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    basename = raw_path.name
    for ext in IMAGE_EXTENSIONS | UNSUPPORTED_EXTENSIONS:
        for match in root_dir.rglob(f"{basename}{ext}" if not raw_path.suffix else basename):
            if match.exists():
                return match
    return None


def _render_pdf_first_page(pdf_path: Path, output_path: Path) -> None:
    import fitz  # type: ignore[import-not-found]

    doc = fitz.open(str(pdf_path))
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(PDF_IMAGE_DPI / 72, PDF_IMAGE_DPI / 72), alpha=False)
    output_path.write_bytes(pix.tobytes("png"))
    doc.close()


def _probe_image_size(path: Path) -> tuple[int, int]:
    if path.suffix.lower() == ".svg":
        text = path.read_text(encoding="utf-8", errors="ignore")[:4000]
        viewbox = re.search(r'viewBox=["\']\s*[\d.\-]+\s+[\d.\-]+\s+([\d.]+)\s+([\d.]+)', text)
        if viewbox:
            return int(float(viewbox.group(1))), int(float(viewbox.group(2)))
    try:
        from PIL import Image

        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:
        return 0, 0


def _pdf_image_infos(page: Any) -> list[dict[str, Any]]:
    try:
        return list(page.get_image_info(xrefs=True))
    except Exception:
        return []


def _pdf_text_blocks(page: Any) -> list[dict[str, Any]]:
    blocks = []
    try:
        raw_blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return blocks
    for block in raw_blocks:
        if block.get("type") != 0:
            continue
        text = " ".join(
            span.get("text", "")
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if span.get("text", "").strip()
        ).strip()
        if not text:
            continue
        blocks.append({"text": text, "rect": block["bbox"], "char_count": len(text)})
    return blocks


def _pdf_caption_blocks(text_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    captions = []
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return captions
    for block in text_blocks:
        text = block["text"].strip()
        if CAPTION_RE.search(text):
            captions.append({"text": text, "rect": fitz.Rect(block["rect"])})
    return captions


def _pdf_meaningful_bbox(page_rect: Any, bbox: Any, image_info: dict[str, Any]) -> bool:
    if bbox.is_empty or bbox.width < MIN_IMAGE_SIDE or bbox.height < MIN_IMAGE_SIDE:
        return False
    if page_rect.get_area() and bbox.get_area() / page_rect.get_area() < 0.01:
        return False
    width = float(image_info.get("width") or 0)
    height = float(image_info.get("height") or 0)
    return not (width and height and (width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE))


def _nearest_caption_text(bbox: Any, captions: list[dict[str, Any]]) -> str:
    best = ""
    best_dist = float("inf")
    for cap in captions:
        rect = cap["rect"]
        dist = min(abs(rect.y0 - bbox.y1), abs(bbox.y0 - rect.y1))
        if dist < best_dist and dist < 260:
            best = cap["text"]
            best_dist = dist
    return best


def _caption_region_clip(page_rect: Any, caption_rect: Any, text_blocks: list[dict[str, Any]]) -> Any | None:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return None
    top = max(page_rect.y0, caption_rect.y0 - min(360, page_rect.height * 0.45))
    for block in text_blocks:
        rect = fitz.Rect(block["rect"])
        if rect.y1 > caption_rect.y0 or rect.y1 < top:
            continue
        if block.get("char_count", 0) > 180 and rect.y1 > top:
            top = max(top, rect.y1 + 4)
    clip = fitz.Rect(
        page_rect.x0 + max(20, page_rect.width * 0.04),
        top,
        page_rect.x1 - max(20, page_rect.width * 0.04),
        caption_rect.y0 - 4,
    )
    if clip.width < MIN_IMAGE_SIDE or clip.height < MIN_IMAGE_SIDE:
        return None
    return clip


def _guess_pdf_title(doc: Any, source_path: Path) -> str:
    meta_title = str(doc.metadata.get("title") or "").strip()
    if meta_title:
        return meta_title
    if len(doc) == 0:
        return source_path.stem
    return _guess_title_from_text(doc[0].get_text("text") or "") or source_path.stem


def _guess_title_from_text(text: str) -> str:
    for line in text.splitlines()[:40]:
        cleaned = re.sub(r"\s+", " ", line).strip()
        if 8 <= len(cleaned) <= 180 and not cleaned.lower().startswith(("abstract", "keywords")):
            return cleaned
    return ""


def _guess_section_from_page_text(text: str) -> str:
    for line in text.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if re.match(r"^(?:\d+(?:\.\d+)*\s+)?[A-Z][A-Za-z0-9 ,:/-]{4,80}$", cleaned):
            return cleaned
    return ""


def _context_lines(text: str) -> tuple[str, str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return (" ".join(lines[:5])[:700], " ".join(lines[-5:])[:700])


def _rect_list(rect: Any) -> list[float]:
    return [round(float(rect.x0), 2), round(float(rect.y0), 2), round(float(rect.x1), 2), round(float(rect.y1), 2)]


def _caption_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _basic_latex_to_markdown(content: str) -> str:
    body = _extract_environment(content, "document") or content
    body = _strip_latex_comments(body)

    def heading(match: re.Match[str]) -> str:
        command = match.group(1)
        level = {"part": "##", "chapter": "##", "section": "##", "subsection": "###", "subsubsection": "####"}[command]
        return f"\n{level} {_clean_latex(match.group(2))}\n"

    body = re.sub(
        r"\\(part|chapter|section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\{([^{}]+)\}",
        heading,
        body,
    )
    body = re.sub(r"\\begin\{(?:figure|table)\*?\}.+?\\end\{(?:figure|table)\*?\}", "", body, flags=re.DOTALL)
    body = re.sub(r"\\begin\{abstract\}.+?\\end\{abstract\}", "", body, flags=re.DOTALL)
    body = re.sub(r"\\item\s*", "- ", body)
    return _clean_latex_block(body)


def _clean_latex_block(text: str) -> str:
    lines = []
    previous_blank = False
    for line in text.splitlines():
        cleaned = _clean_latex(line)
        if cleaned:
            lines.append(cleaned)
            previous_blank = False
        elif lines and not previous_blank:
            lines.append("")
            previous_blank = True
    return "\n".join(lines).strip()


def _clean_latex(text: str) -> str:
    text = re.sub(r"~", " ", text or "")
    text = re.sub(r"\\(?:cite|citep|citet|ref|eqref|label)\*?(?:\[[^\]]*\])?\{[^}]*\}", "", text)
    text = re.sub(r"\\(?:begin|end)\{[^}]+\}", "", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", "", text)
    text = re.sub(r"\\([{}%&_#])", r"\1", text)
    text = re.sub(r"[{}]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _append_figure_tokens(lines: list[str], figures: list[FigureRecord]) -> None:
    available = [fig for fig in figures if fig.available]
    if not available:
        return
    lines.extend(["", "## Extracted Figure Inventory", ""])
    for fig in available:
        caption = fig.caption or "No caption found"
        location = f"page {fig.page_number}" if fig.page_number else "TeX source"
        size = f"{fig.natural_width}x{fig.natural_height}px" if fig.natural_width and fig.natural_height else "size unknown"
        lines.append(f"[[FIG:{fig.id}]] {location}; {size}; href `{fig.href}`; caption: {caption}")


def _format_figures_markdown(figures: list[FigureRecord]) -> str:
    lines = [
        "# Figure Inventory",
        "",
        "Use the `href` exactly as written in slide SVG `<image href=\"...\">` elements.",
        "Choose figures by caption, page/location, and context rather than filename alone.",
        "",
        "| id | href | location | size | caption | context |",
        "|----|------|----------|------|---------|---------|",
    ]
    for fig in figures:
        if not fig.available:
            note = "; ".join(fig.notes or ["unavailable"])
            lines.append(f"| {fig.id} | unavailable | - | - | {_md_cell(fig.caption)} | {_md_cell(note)} |")
            continue
        location = f"p{fig.page_number}" if fig.page_number else "TeX"
        size = f"{fig.natural_width}x{fig.natural_height}" if fig.natural_width and fig.natural_height else "?"
        context = fig.section_hint or fig.context_before or fig.extraction_method
        lines.append(
            f"| `{fig.id}` | `{fig.href}` | {location} | {size} | {_md_cell(fig.caption)} | {_md_cell(context)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _asset_readme() -> str:
    return """# Source Assets

- `paper.md`: extracted source text with `[[FIG:id]]` anchors.
- `figures.json`: machine-readable figure records with captions, page/location, bbox, dimensions, and SVG-ready hrefs.
- `figures.md`: human-readable figure inventory for slide planning.
- `images/`: prepared local images. From `svg_output/*.svg`, reference them with the `href` values in `figures.json`.
"""


def _md_cell(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").replace("|", "\\|")[:240]


def _ratio(width: int, height: int) -> float:
    return round(width / height, 4) if width > 0 and height > 0 else 0.0


def _posix(path: Path) -> str:
    return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
