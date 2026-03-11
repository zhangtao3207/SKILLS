#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sanitize_module_slug(module: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", module.lower()).strip("_")
    return slug or "module"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def looks_like_gerber_zip(path: Path) -> bool:
    if path.suffix.lower() != ".zip":
        return False
    name = path.name.lower()
    return any(k in name for k in ("gerber", "pcb", "cam"))


def prefer_pcb_data_dir(base_dir: Path) -> Path:
    try:
        b = base_dir.resolve()
    except Exception:
        b = base_dir
    if b.name.lower() == "pcb_data":
        return b
    cand = b / "pcb_data"
    return cand if cand.exists() and cand.is_dir() else b


def find_latest_gerber_zip(module_dir: Path) -> Path | None:
    zips = [p for p in module_dir.rglob("*.zip") if p.is_file()]
    if not zips:
        return None
    preferred = [p for p in zips if looks_like_gerber_zip(p)]
    candidates = preferred if preferred else zips
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether gerber zip changed since last run")
    parser.add_argument("--module", required=True, help="Module name")
    parser.add_argument("--gerber", type=Path, default=None, help="Gerber zip file. Optional, auto-detect latest when omitted")
    parser.add_argument("--workspace-dir", type=Path, default=Path.cwd(), help="Workspace/module directory for Gerber auto-detection")
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tmp" / "state",
        help="State root folder",
    )
    args = parser.parse_args()

    module_dir = prefer_pcb_data_dir(args.workspace_dir)
    gerber = args.gerber if args.gerber else find_latest_gerber_zip(module_dir)
    if gerber is None:
        print(json.dumps({"changed": False, "reason": "gerber_not_found", "path": "", "message": "未找到Gerber压缩包 请提供--gerber或放入模块目录"}, ensure_ascii=False))
        return 1
    gerber = gerber.resolve()
    if not gerber.exists():
        print(json.dumps({"changed": False, "reason": "gerber_not_found", "path": str(gerber), "message": "指定Gerber不存在"}, ensure_ascii=False))
        return 1

    module_slug = sanitize_module_slug(args.module)
    module_state_dir = args.state_root / module_slug
    module_state_dir.mkdir(parents=True, exist_ok=True)
    state_file = module_state_dir / "gerber_watch_state.json"

    current = {
        "path": str(gerber),
        "size": gerber.stat().st_size,
        "mtime_ns": gerber.stat().st_mtime_ns,
        "sha256": file_sha256(gerber),
    }

    previous = {}
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    changed = (
        previous.get("sha256") != current["sha256"]
        or previous.get("size") != current["size"]
        or previous.get("mtime_ns") != current["mtime_ns"]
        or previous.get("path") != current["path"]
    )

    state_file.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "changed": changed,
                "module": module_slug,
                "gerber": str(gerber),
                "state_file": str(state_file),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
