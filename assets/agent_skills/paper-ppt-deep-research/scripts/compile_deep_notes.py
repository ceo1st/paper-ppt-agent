from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Index Agent/SubAgent deep research notes.")
    parser.add_argument("--notes-dir", default="research/deep/notes")
    parser.add_argument("--out", default="research/deep/notes_index.json")
    args = parser.parse_args()

    notes_dir = Path(args.notes_dir)
    rows = []
    if notes_dir.exists():
        for path in sorted(notes_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            rows.append(
                {
                    "file": str(path),
                    "title": _title(text, path.stem),
                    "anchors": _anchors(text),
                    "summary_excerpt": _excerpt(text),
                }
            )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"notes": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_path))
    return 0


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


def _anchors(text: str) -> list[str]:
    patterns = [
        r"\b(?:Figure|Fig\.|Table|Equation|Eq\.)\s*\d+[A-Za-z]?\b",
        r"\bpage\s+\d+\b",
        r"\bsection\s+\d+(?:\.\d+)*\b",
    ]
    found: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            normalized = re.sub(r"\s+", " ", match).strip()
            key = normalized.lower()
            if key not in seen:
                seen.add(key)
                found.append(normalized)
            if len(found) >= 20:
                return found
    return found


def _excerpt(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", re.sub(r"```.*?```", "", text, flags=re.DOTALL)).strip()
    return cleaned[:1200]


if __name__ == "__main__":
    raise SystemExit(main())
