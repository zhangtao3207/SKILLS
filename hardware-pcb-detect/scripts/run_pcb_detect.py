#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from pcb_detect_component import (
    build_cost_table_data,
    component_identity_key,
    component_signature,
    extract_lcsc_code_from_text,
    load_component_info_state,
    normalize_lcsc_code,
    parse_bom_cost,
    save_component_info_state,
    sync_datasheets,
    trim_component_state,
)
from pcb_detect_report import estimate_power, write_outputs

MM_PER_MIL = 0.0254
GERBER_TEXT_SUFFIX = {
    ".gtl", ".gbl", ".g1", ".g2", ".g3", ".g4", ".gko",
    ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".drl", ".txt", ".json"
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PCB feasibility checks from Gerber and netlist files")
    parser.add_argument("--module", required=True, help="Module name used in output filenames")
    parser.add_argument("--gerber", type=Path, default=None, help="Gerber zip path, optional. If omitted, auto-select latest Gerber zip")
    parser.add_argument("--netlist", required=True, type=Path, help="Netlist .tel path")
    parser.add_argument("--doc", action="append", default=[], type=Path, help="Design text file path, repeatable")
    parser.add_argument("--pnp", type=Path, default=None, help="Optional pick and place file path")
    parser.add_argument("--bom", type=Path, default=None, help="Optional BOM path csv/xlsx/xls")
    parser.add_argument("--workspace-dir", type=Path, default=None, help="Workspace/module directory used for auto-discovery")
    parser.add_argument("--output-dir", type=Path, default=None, help="Log directory when --log-mode on")
    parser.add_argument("--log-mode", choices=("off", "on"), default="off", help="Whether to write task logs to disk")
    parser.add_argument("--datasheet-dir", type=Path, default=None, help="Datasheet folder path, default is <module_dir>/datasheet")
    parser.add_argument("--datasheet-auto", choices=("on", "off"), default="on", help="Auto-download datasheets")
    parser.add_argument("--price-auto", choices=("on", "off"), default="on", help="Auto-query unit price by LCSC code or model when BOM has no usable price")
    parser.add_argument("--file-manage", choices=("ask", "yes", "no"), default="ask", help="Whether to ask/apply file management suggestions")
    parser.add_argument("--file-manage-apply", action="store_true", help="Apply file organization moves after confirmation mode")
    parser.add_argument("--mains-rms-max", type=float, default=264.0, help="Max mains RMS for worst-case checks")
    parser.add_argument(
        "--analysis-profile",
        choices=("auto", "generic"),
        default="auto",
        help="Analysis profile: generic for broad hardware checks. auto is a compatibility alias of generic",
    )
    return parser.parse_args()


def now_stamp() -> str:
    return dt.datetime.now().strftime("%y%m%d_%H%M")


def sanitize_module_slug(module: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", module.lower()).strip("_")
    return slug or "module"


def prefer_pcb_data_dir(base_dir: Path) -> Path:
    try:
        b = base_dir.resolve()
    except Exception:
        b = base_dir
    if b.name.lower() == "pcb_data":
        return b
    cand = b / "pcb_data"
    return cand if cand.exists() and cand.is_dir() else b


def prefer_user_report_dir(data_dir: Path) -> Path:
    try:
        d = data_dir.resolve()
    except Exception:
        d = data_dir
    if d.name.lower() == "pcb_data" and d.parent.exists():
        return d.parent
    return d


def looks_like_gerber_zip(path: Path) -> bool:
    if path.suffix.lower() != ".zip":
        return False
    name = path.name.lower()
    return any(k in name for k in ("gerber", "pcb", "cam"))


def resolve_module_dir(args: argparse.Namespace) -> Path:
    if args.workspace_dir is not None:
        return prefer_pcb_data_dir(args.workspace_dir)
    if args.netlist is not None:
        return prefer_pcb_data_dir(args.netlist.resolve().parent)
    return prefer_pcb_data_dir(Path.cwd())


def find_latest_gerber_zip(module_dir: Path) -> Optional[Path]:
    zips = [p for p in module_dir.rglob("*.zip") if p.is_file()]
    if not zips:
        return None
    preferred = [p for p in zips if looks_like_gerber_zip(p)]
    candidates = preferred if preferred else zips
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def resolve_gerber_path(args: argparse.Namespace, module_dir: Path) -> Optional[Path]:
    if args.gerber is not None:
        p = args.gerber if args.gerber.is_absolute() else (module_dir / args.gerber)
        return p.resolve() if p.exists() else None
    return find_latest_gerber_zip(module_dir)


def looks_like_bom_file(path: Path) -> bool:
    if path.suffix.lower() not in (".csv", ".xlsx", ".xls"):
        return False
    deny_tokens = ("pick", "pnp", "position", "layer", "nets_summary", "components", "pins", "readme")
    name = path.name.lower()
    if any(t in name for t in deny_tokens):
        return False
    deny_dirs = ("gerber_analyse", ".codex", "datasheet", "log", "tmp")
    p = str(path).lower()
    return not any(d in p for d in deny_dirs)


def score_bom_candidate(path: Path) -> int:
    score = 0
    name = path.name.lower()
    if "bom" in name:
        score += 100
    if "bill" in name and "material" in name:
        score += 80
    if any(k in name for k in ("物料", "清单")):
        score += 60
    if path.suffix.lower() == ".csv":
        score += 20
        try:
            head = read_text(path).splitlines()[:2]
            hdr = ",".join(head).lower()
            if any(k in hdr for k in ("qty", "quantity", "count", "数量")):
                score += 30
            if any(k in hdr for k in ("price", "unit price", "amount", "total", "金额", "单价")):
                score += 30
        except Exception:
            pass
    elif path.suffix.lower() == ".xlsx":
        score += 15
    elif path.suffix.lower() == ".xls":
        score += 5
    return score


def find_latest_bom_file(module_dir: Path) -> Optional[Path]:
    files = [p for p in module_dir.rglob("*") if p.is_file() and looks_like_bom_file(p)]
    if not files:
        return None
    files.sort(key=lambda p: (score_bom_candidate(p), p.stat().st_mtime), reverse=True)
    return files[0]


def resolve_bom_path(args: argparse.Namespace, module_dir: Path) -> Optional[Path]:
    if args.bom is not None:
        p = args.bom if args.bom.is_absolute() else (module_dir / args.bom)
        return p.resolve() if p.exists() else None
    return find_latest_bom_file(module_dir)


def read_text(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin1"):
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return path.read_text(errors="ignore")


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode(errors="ignore")


def likely_part_number(token: str) -> bool:
    s = token.strip()
    if len(s) < 5:
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    if not re.search(r"[0-9]", s):
        return False
    weak = ("ohm", "res", "cap", "uf", "nf", "pf", "mhz", "khz", "v", "a")
    low = s.lower()
    return not (len(low) <= 8 and any(low.endswith(w) for w in weak))


def parse_value_ohm(raw: str) -> Optional[float]:
    if not raw:
        return None
    s = raw.strip()
    for token in ("\u03a9", "Ω", "欧", "姆", "惟", "'", '"'):
        s = s.replace(token, "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kKmMuUnNpP]?)", s)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "k":
        val *= 1e3
    elif unit == "m":
        val *= 1e-3
    elif unit == "u":
        val *= 1e-6
    elif unit == "n":
        val *= 1e-9
    elif unit == "p":
        val *= 1e-12
    return val if val > 0 else None


def collect_entries(block: str) -> List[str]:
    entries: List[str] = []
    buf = ""
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(","):
            line = line[1:].strip()
        if not line:
            continue
        buf = f"{buf} {line}".strip() if buf else line
        if ";" in buf and not buf.endswith(","):
            entries.append(buf)
            buf = ""
    if buf and ";" in buf:
        entries.append(buf)
    return entries


def get_block(text: str, start_marker: str, end_marker: str) -> str:
    if start_marker not in text or end_marker not in text:
        return ""
    return text.split(start_marker, 1)[1].split(end_marker, 1)[0]


def parse_packages(text: str) -> Tuple[Dict[str, str], Dict[str, float]]:
    block = get_block(text, "$PACKAGES", "$A_PROPERTIES")
    entries = collect_entries(block)
    ref_to_raw_value: Dict[str, str] = {}
    resistor_ohm: Dict[str, float] = {}
    for entry in entries:
        if ";" not in entry:
            continue
        left, right = entry.split(";", 1)
        parts = [p.strip() for p in left.split("!")]
        value_raw = parts[2] if len(parts) >= 3 else ""
        refs = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", right)
        for ref in refs:
            ref_to_raw_value[ref] = value_raw
            if ref.startswith("R"):
                val = parse_value_ohm(value_raw)
                if val is not None:
                    resistor_ohm[ref] = val
    return ref_to_raw_value, resistor_ohm


def detect_analysis_profile(requested: str) -> str:
    if requested in ("auto", "generic"):
        return "generic"
    return "generic"


def parse_nets(text: str) -> Dict[str, List[str]]:
    block = get_block(text, "$NETS", "$SCHEDULE")
    entries = collect_entries(block)
    nets: Dict[str, List[str]] = {}
    for entry in entries:
        if ";" not in entry:
            continue
        left, right = entry.split(";", 1)
        net = left.strip().strip("'").strip()
        pins: List[str] = []
        for part in right.split(","):
            for token in part.strip().split():
                t = token.strip()
                if "." in t and re.match(r"^[A-Za-z0-9_\$\+#\-]+\.[A-Za-z0-9_#]+$", t):
                    pins.append(t)
        nets[net] = pins
    return nets


def pick_col_name(cols: Dict[str, str], keywords: List[str]) -> Optional[str]:
    for key in keywords:
        for c_lower, c_orig in cols.items():
            if key in c_lower:
                return c_orig
    return None


def xlsx_col_to_index(col_letters: str) -> int:
    v = 0
    for ch in col_letters:
        if "A" <= ch <= "Z":
            v = v * 26 + (ord(ch) - ord("A") + 1)
    return max(v - 1, 0)


def parse_xlsx_rows(path: Path) -> List[Dict[str, str]]:
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        if "xl/workbook.xml" not in names:
            return []

        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(".//{*}si"):
                texts = [t.text or "" for t in si.findall(".//{*}t")]
                shared_strings.append("".join(texts))

        sheet_path = "xl/worksheets/sheet1.xml"
        if sheet_path not in names:
            sheets = sorted([n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")])
            if not sheets:
                return []
            sheet_path = sheets[0]

        sroot = ET.fromstring(zf.read(sheet_path))
        rows_raw: List[Dict[int, str]] = []
        for row in sroot.findall(".//{*}sheetData/{*}row"):
            row_map: Dict[int, str] = {}
            for c in row.findall("{*}c"):
                ref = c.attrib.get("r", "")
                m = re.match(r"([A-Z]+)\d+", ref)
                if not m:
                    continue
                col_idx = xlsx_col_to_index(m.group(1))
                ctype = c.attrib.get("t", "")
                val = ""
                if ctype == "inlineStr":
                    tnode = c.find(".//{*}t")
                    val = (tnode.text or "") if tnode is not None else ""
                else:
                    vnode = c.find("{*}v")
                    raw = (vnode.text or "") if vnode is not None else ""
                    if ctype == "s":
                        try:
                            sval = shared_strings[int(raw)]
                            val = sval
                        except Exception:
                            val = raw
                    else:
                        val = raw
                row_map[col_idx] = val.strip()
            if row_map:
                rows_raw.append(row_map)

        if not rows_raw:
            return []

        header_map = rows_raw[0]
        max_col = max(max(r.keys()) for r in rows_raw)
        headers: List[str] = []
        for i in range(max_col + 1):
            h = (header_map.get(i, "") or "").strip()
            headers.append(h if h else f"COL_{i+1}")

        out: List[Dict[str, str]] = []
        for r in rows_raw[1:]:
            row_dict: Dict[str, str] = {}
            has_data = False
            for i, h in enumerate(headers):
                v = r.get(i, "")
                if v:
                    has_data = True
                row_dict[h] = v
            if has_data:
                out.append(row_dict)
        return out


def parse_csv_rows(path: Path) -> List[Dict[str, str]]:
    text = read_text(path)
    return list(csv.DictReader(text.splitlines()))


def parse_tsv_like_rows(path: Path) -> List[Dict[str, str]]:
    text = read_text(path)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    sep = "\t" if lines[0].count("\t") >= lines[0].count(",") else ","
    headers = [h.strip() or f"COL_{i+1}" for i, h in enumerate(lines[0].split(sep))]
    out: List[Dict[str, str]] = []
    for line in lines[1:]:
        vals = [v.strip() for v in line.split(sep)]
        row: Dict[str, str] = {}
        has_data = False
        for i, h in enumerate(headers):
            v = vals[i] if i < len(vals) else ""
            if v:
                has_data = True
            row[h] = v
        if has_data:
            out.append(row)
    return out


def load_bom_rows(bom_path: Optional[Path]) -> Tuple[List[Dict[str, str]], str]:
    if not bom_path:
        return [], "BOM not provided"
    if not bom_path.exists():
        return [], f"BOM missing: {bom_path}"

    ext = bom_path.suffix.lower()
    try:
        if ext == ".csv":
            rows = parse_csv_rows(bom_path)
            return rows, "ok" if rows else "BOM CSV has no rows"
        if ext == ".xlsx":
            rows = parse_xlsx_rows(bom_path)
            return rows, "ok" if rows else "BOM XLSX has no rows"
        if ext == ".xls":
            rows = parse_tsv_like_rows(bom_path)
            if rows:
                return rows, "ok"
            return [], "BOM XLS not supported directly please export CSV or XLSX"
        rows = parse_tsv_like_rows(bom_path)
        if rows:
            return rows, "ok"
    except Exception as e:
        return [], f"BOM parse failed: {e}"
    return [], f"Unsupported BOM format: {ext}"


def parse_bom_components(bom_path: Optional[Path]) -> List[dict]:
    rows, _ = load_bom_rows(bom_path)
    if not rows:
        return []
    cols = {c.lower().strip(): c for c in rows[0].keys() if c}
    col_ref = pick_col_name(cols, ["designator", "reference", "refdes", "ref", "位号"])
    col_mpn = pick_col_name(cols, ["manufacturer part", "mpn", "part number", "model", "型号"])
    col_lcsc = pick_col_name(cols, ["lcsc", "jlcpcb", "supplier part"])
    col_desc = pick_col_name(cols, ["description", "comment", "desc", "value"])
    col_datasheet = pick_col_name(cols, ["datasheet", "data sheet", "规格书"])
    col_url = pick_col_name(cols, ["url", "link", "product page", "商品链接"])

    comps: List[dict] = []
    for row in rows:
        lcsc = str(row.get(col_lcsc, "")).strip() if col_lcsc else ""
        mpn = str(row.get(col_mpn, "")).strip() if col_mpn else ""
        desc = str(row.get(col_desc, "")).strip() if col_desc else ""
        durl = str(row.get(col_datasheet, "")).strip() if col_datasheet else ""
        purl = str(row.get(col_url, "")).strip() if col_url else ""
        ref = str(row.get(col_ref, "")).strip() if col_ref else ""
        if not (lcsc or mpn or durl or purl):
            continue
        key = component_identity_key(lcsc=lcsc, mpn=mpn, ref=ref, desc=desc)
        sig = component_signature(lcsc=lcsc, mpn=mpn, ref=ref, desc=desc, datasheet_url=durl, product_url=purl)
        comps.append(
            {
                "source": "bom",
                "ref": ref,
                "lcsc": lcsc,
                "mpn": mpn,
                "desc": desc,
                "datasheet_url": durl,
                "product_url": purl,
                "component_key": key,
                "component_sig": sig,
            }
        )
    return comps


def collect_netlist_part_candidates(ref_to_raw_value: Dict[str, str]) -> List[dict]:
    by_part: Dict[str, List[str]] = {}
    for ref, raw in ref_to_raw_value.items():
        token = raw.strip()
        if not token or not likely_part_number(token):
            continue
        by_part.setdefault(token, []).append(ref)
    out: List[dict] = []
    for part, refs in sorted(by_part.items(), key=lambda kv: kv[0]):
        out.append(
            {
                "source": "netlist",
                "ref": ",".join(refs[:8]),
                "lcsc": "",
                "mpn": part,
                "desc": "",
                "datasheet_url": "",
                "product_url": "",
            }
        )
    return out


def collect_netlist_simple_components(ref_to_raw_value: Dict[str, str]) -> List[dict]:
    out: List[dict] = []
    for ref, raw in sorted(ref_to_raw_value.items(), key=lambda kv: kv[0]):
        if not ref:
            continue
        prefix = re.match(r"^[A-Za-z]+", ref)
        p = prefix.group(0).upper() if prefix else ""
        if p not in {"R", "RN", "C", "L", "SW", "KEY", "BTN", "FB", "RV", "VR", "NTC", "PTC"}:
            continue
        out.append(
            {
                "source": "netlist_simple",
                "ref": ref,
                "lcsc": "",
                "mpn": raw.strip(),
                "desc": "",
                "datasheet_url": "",
                "product_url": "",
            }
        )
    return out


def dedup_components(components: List[dict]) -> List[dict]:
    merged: Dict[str, dict] = {}
    for comp in components:
        key = (comp.get("component_key") or "").strip()
        if not key:
            key = component_identity_key(
                lcsc=str(comp.get("lcsc", "")),
                mpn=str(comp.get("mpn", "")),
                ref=str(comp.get("ref", "")),
                desc=str(comp.get("desc", "")),
            )
        if not key:
            continue
        k = key.upper()
        if k not in merged:
            merged[k] = dict(comp)
            merged[k]["component_key"] = key
            merged[k]["component_sig"] = component_signature(
                lcsc=str(comp.get("lcsc", "")),
                mpn=str(comp.get("mpn", "")),
                ref=str(comp.get("ref", "")),
                desc=str(comp.get("desc", "")),
                datasheet_url=str(comp.get("datasheet_url", "")),
                product_url=str(comp.get("product_url", "")),
            )
            continue
        for field in ("datasheet_url", "product_url", "lcsc", "mpn", "desc"):
            if not merged[k].get(field) and comp.get(field):
                merged[k][field] = comp[field]
        if merged[k].get("ref") and comp.get("ref"):
            merged[k]["ref"] = f"{merged[k]['ref']},{comp['ref']}"
        merged[k]["component_sig"] = component_signature(
            lcsc=str(merged[k].get("lcsc", "")),
            mpn=str(merged[k].get("mpn", "")),
            ref=str(merged[k].get("ref", "")),
            desc=str(merged[k].get("desc", "")),
            datasheet_url=str(merged[k].get("datasheet_url", "")),
            product_url=str(merged[k].get("product_url", "")),
        )
    return list(merged.values())


def classify_workspace_file(path: Path) -> str:
    name = path.name.lower()
    ext = path.suffix.lower()
    if ext == ".zip" and looks_like_gerber_zip(path):
        return "gerber"
    if ext == ".tel":
        return "netlist"
    if "pick" in name or "pnp" in name or "place" in name:
        return "pnp"
    if "bom" in name and ext in (".csv", ".xlsx", ".xls", ".txt"):
        return "bom"
    if ext in (".txt", ".md", ".doc", ".docx", ".pdf"):
        return "docs"
    return "others"


def build_file_management_plan(module_dir: Path) -> List[Tuple[Path, Path]]:
    plan: List[Tuple[Path, Path]] = []
    in_pcb_data = module_dir.name.lower() == "pcb_data"
    for path in sorted(module_dir.iterdir(), key=lambda p: p.name.lower()):
        if path.is_dir():
            continue
        if path.name.startswith("tasklist") or path.name.startswith("任务清单"):
            continue
        if path.name.startswith("_datasheet_sync_report"):
            continue
        bucket = classify_workspace_file(path)
        if bucket == "others":
            continue
        target_dir = module_dir if in_pcb_data else (module_dir / bucket)
        target_name = re.sub(r"\s+", "_", path.name).strip("_") if in_pcb_data else re.sub(r"\s+", "_", path.name).strip("_")
        target = target_dir / target_name
        if path.resolve() == target.resolve():
            continue
        plan.append((path, target))
    return plan


def detect_workspace_clutter(module_dir: Path) -> Tuple[bool, List[str], List[Tuple[Path, Path]]]:
    root_files = [p for p in module_dir.iterdir() if p.is_file()]
    plan = build_file_management_plan(module_dir)
    reasons: List[str] = []
    if len(root_files) >= 12:
        reasons.append(f"根目录文件较多 {len(root_files)}")
    if len(plan) >= 5:
        reasons.append(f"可整理项较多 {len(plan)}")
    noisy = [p for p in root_files if " " in p.name or p.name.count("_") >= 4]
    if len(noisy) >= 4:
        reasons.append(f"文件命名不统一 {len(noisy)}")
    return (len(reasons) > 0), reasons, plan


def apply_file_management_plan(plan: List[Tuple[Path, Path]]) -> Tuple[int, int]:
    moved = 0
    failed = 0
    for src, dst in plan:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            final = dst
            idx = 1
            while final.exists() and final.resolve() != src.resolve():
                final = dst.with_name(f"{dst.stem}_{idx}{dst.suffix}")
                idx += 1
            src.rename(final)
            moved += 1
        except Exception:
            failed += 1
    return moved, failed


def handle_file_management(
    args: argparse.Namespace,
    module_dir: Path,
    findings: List[dict],
    metrics: Dict[str, object],
) -> None:
    cluttered, reasons, plan = detect_workspace_clutter(module_dir)
    metrics["file_management_detect"] = {"cluttered": cluttered, "reasons": reasons, "plan_items": len(plan)}
    if not cluttered:
        add_finding(
            findings,
            "info",
            "Workspace file layout looks clean",
            "No obvious clutter in module workspace. Skip file management prompt",
            "Continue current file structure",
            str(module_dir),
        )
        return

    decision = args.file_manage
    if decision == "ask":
        if sys.stdin.isatty():
            reason_text = " ".join(reasons) if reasons else "检测到目录可能较乱"
            try:
                answer = input(f"检测到工作区可能较乱 {reason_text} 是否进行文件管理整理 [y/N]: ").strip().lower()
            except EOFError:
                answer = ""
            decision = "yes" if answer in ("y", "yes", "1", "是") else "no"
        else:
            add_finding(
                findings,
                "medium",
                "File management pending user decision",
                f"Workspace appears cluttered. {' '.join(reasons)}",
                "Use --file-manage yes and optionally --file-manage-apply",
                str(module_dir),
            )
            return

    if decision == "no":
        add_finding(
            findings,
            "info",
            "File management skipped by user",
            f"User chose not to reorganize workspace files. {' '.join(reasons)}",
            "Re-run with --file-manage yes if needed",
            str(module_dir),
        )
        return

    plan_file = module_dir / "file_management_plan.txt"
    lines = ["文件管理计划", f"模块目录 {module_dir}", f"计划项数量 {len(plan)}", ""]
    for i, (src, dst) in enumerate(plan, 1):
        lines.append(f"{i} {src.name} -> {dst.relative_to(module_dir)}")
    plan_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    moved = 0
    failed = 0
    if args.file_manage_apply and plan:
        moved, failed = apply_file_management_plan(plan)

    metrics["file_management"] = {
        "plan_file": str(plan_file),
        "items": len(plan),
        "apply": bool(args.file_manage_apply),
        "moved": moved,
        "failed": failed,
    }
    add_finding(
        findings,
        "low",
        "File management plan generated",
        f"Plan items {len(plan)}, apply={args.file_manage_apply}, moved={moved}, failed={failed}",
        "Review file_management_plan.txt before batch operations",
        str(plan_file),
    )


def build_component_pin_net_map(nets: Dict[str, List[str]]) -> Dict[str, Dict[str, str]]:
    mapping: Dict[str, Dict[str, str]] = {}
    for net, pins in nets.items():
        for pin in pins:
            comp, pin_no = pin.split(".", 1)
            mapping.setdefault(comp, {})[pin_no] = net
    return mapping


def netset_from_mapping(nets: Dict[str, List[str]]) -> set:
    return set(nets.keys())


def add_finding(findings: List[dict], severity: str, title: str, detail: str, action: str, source: str = "") -> None:
    findings.append({"severity": severity.lower(), "title": title, "detail": detail, "action": action, "source": source})

def classify_gerber_role(name: str) -> str:
    n = name.lower()
    if n.endswith(".gtl"):
        return "top_copper"
    if n.endswith(".gbl"):
        return "bottom_copper"
    if re.search(r"\.g\d+$", n):
        return "inner_copper"
    if n.endswith(".gko"):
        return "board_outline"
    if n.endswith(".gts"):
        return "top_mask"
    if n.endswith(".gbs"):
        return "bottom_mask"
    if n.endswith(".gto"):
        return "top_silk"
    if n.endswith(".gbo"):
        return "bottom_silk"
    if n.endswith(".gtp"):
        return "top_paste"
    if n.endswith(".gbp"):
        return "bottom_paste"
    if n.endswith(".drl"):
        return "drill"
    if n.endswith("flyingprobetesting.json"):
        return "flying_probe"
    if n.endswith(".txt"):
        return "notes"
    return "other"


def summarize_gerber_text(text: str) -> Dict[str, int]:
    lines = text.splitlines()
    return {
        "line_count": len(lines),
        "g_cmd_count": sum(1 for line in lines if line.startswith("G")),
        "d_code_count": len(re.findall(r"\bD\d{2,3}\b", text)),
        "coord_count": len(re.findall(r"X-?\d+Y-?\d+", text)),
        "aperture_count": len(re.findall(r"%ADD", text)),
    }


def write_csv(path: Path, headers: List[str], rows: List[List[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def export_flying_probe_tables(flying_probe: Optional[dict], out_dir: Path) -> List[str]:
    generated: List[str] = []
    comp_headers = ["component_no", "component_name", "layer", "x_mil", "y_mil", "angle"]
    comp_rows: List[List[object]] = []
    pin_headers = ["pin_no", "pin_name", "net_name", "layer", "x_mil", "y_mil", "pad_sizex_mil", "pad_sizey_mil"]
    pin_rows: List[List[object]] = []
    nets_rows: List[List[object]] = []

    if flying_probe:
        comps = flying_probe.get("components", {})
        c_fields = comps.get("fields", [])
        c_rows = comps.get("rows", [])
        c_idx = {k: i for i, k in enumerate(c_fields)}
        if all(k in c_idx for k in ["COMPONENT_NO", "COMPONENT_NAME", "LAYER", "X_COORDINATE", "Y_COORDINATE", "ANGLE"]):
            for row in c_rows:
                comp_rows.append([row[c_idx["COMPONENT_NO"]], row[c_idx["COMPONENT_NAME"]], row[c_idx["LAYER"]], row[c_idx["X_COORDINATE"]], row[c_idx["Y_COORDINATE"]], row[c_idx["ANGLE"]]])

        pins = flying_probe.get("pins", {})
        p_fields = pins.get("fields", [])
        p_rows = pins.get("rows", [])
        p_idx = {k: i for i, k in enumerate(p_fields)}
        if all(k in p_idx for k in ["PIN_NO", "PIN_NAME", "NET_NAME", "LAYER", "PIN_X", "PIN_Y", "PAD_SIZEX", "PAD_SIZEY"]):
            for row in p_rows:
                pin_rows.append([row[p_idx["PIN_NO"]], row[p_idx["PIN_NAME"]], row[p_idx["NET_NAME"]], row[p_idx["LAYER"]], row[p_idx["PIN_X"]], row[p_idx["PIN_Y"]], row[p_idx["PAD_SIZEX"]], row[p_idx["PAD_SIZEY"]]])

            net_map: Dict[str, Dict[str, float]] = {}
            for row in pin_rows:
                net = str(row[2]).strip()
                if not net:
                    continue
                x = float(row[4])
                y = float(row[5])
                rec = net_map.setdefault(net, {"count": 0, "min_x": x, "max_x": x, "min_y": y, "max_y": y})
                rec["count"] += 1
                rec["min_x"] = min(rec["min_x"], x)
                rec["max_x"] = max(rec["max_x"], x)
                rec["min_y"] = min(rec["min_y"], y)
                rec["max_y"] = max(rec["max_y"], y)
            for net, rec in sorted(net_map.items(), key=lambda kv: kv[0]):
                nets_rows.append([net, rec["count"], rec["min_x"], rec["max_x"], rec["min_y"], rec["max_y"]])

    comp_path = out_dir / "02_components.csv"
    pin_path = out_dir / "03_pins.csv"
    nets_path = out_dir / "04_nets_summary.csv"
    write_csv(comp_path, comp_headers, comp_rows)
    write_csv(pin_path, pin_headers, pin_rows)
    write_csv(nets_path, ["net_name", "pin_count", "min_x_mil", "max_x_mil", "min_y_mil", "max_y_mil"], nets_rows)
    generated.extend([comp_path.name, pin_path.name, nets_path.name])
    return generated


def build_gerber_analysis_bundle(gerber_zip: Path, module: str, findings: List[dict], metrics: Dict[str, object]) -> Optional[dict]:
    if not gerber_zip.exists():
        add_finding(findings, "high", "Gerber missing", f"File not found: {gerber_zip}", "Provide valid Gerber zip")
        return None

    module_slug = sanitize_module_slug(module)
    out_dir = gerber_zip.parent / f"{module_slug}_gerber_analyse"
    out_dir.mkdir(parents=True, exist_ok=True)

    flying_probe: Optional[dict] = None
    file_manifest: List[dict] = []
    layer_rows: List[List[object]] = []

    with zipfile.ZipFile(gerber_zip, "r") as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        for info in infos:
            name = info.filename
            role = classify_gerber_role(name)
            suffix = Path(name).suffix.lower()
            row = {"file_name": name, "role": role, "size_bytes": info.file_size, "line_count": "", "g_cmd_count": "", "d_code_count": "", "coord_count": "", "aperture_count": ""}
            if suffix in GERBER_TEXT_SUFFIX:
                row.update(summarize_gerber_text(decode_bytes(zf.read(name))))
            if name.lower().endswith("flyingprobetesting.json"):
                try:
                    flying_probe = json.loads(decode_bytes(zf.read(name)))
                except Exception:
                    flying_probe = None
            file_manifest.append({"file_name": name, "role": role, "size_bytes": info.file_size})
            layer_rows.append([row["file_name"], row["role"], row["size_bytes"], row["line_count"], row["g_cmd_count"], row["d_code_count"], row["coord_count"], row["aperture_count"]])

    lower_names = [m["file_name"].lower() for m in file_manifest]
    has = {
        "top_copper": any(n.endswith(".gtl") for n in lower_names),
        "bottom_copper": any(n.endswith(".gbl") for n in lower_names),
        "outline": any(n.endswith(".gko") for n in lower_names),
        "drill": any(n.endswith(".drl") for n in lower_names),
        "top_mask": any(n.endswith(".gts") for n in lower_names),
        "bottom_mask": any(n.endswith(".gbs") for n in lower_names),
    }
    copper_layers = sum(1 for n in lower_names if re.search(r"\.(gtl|gbl|g\d+)$", n))
    missing_required = [k for k in ("top_copper", "bottom_copper", "outline", "drill") if not has[k]]

    metrics["copper_layers"] = copper_layers
    metrics["gerber_file_count"] = len(file_manifest)

    if missing_required:
        add_finding(findings, "high", "Gerber layer completeness failure", f"Missing required layers: {', '.join(missing_required)}", "Regenerate Gerber with complete copper outline and drill outputs", str(gerber_zip))
    else:
        add_finding(findings, "low", "Gerber key layers present", f"Copper layers detected: {copper_layers}", "Proceed to spacing and electrical checks", str(gerber_zip))

    manifest = {
        "module": module_slug,
        "source_gerber_zip": str(gerber_zip),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "file_count": len(file_manifest),
        "files": file_manifest,
        "recommended_fast_formats": ["JSON for metadata index", "CSV for tabular pins nets and components", "TXT for concise guide and notes"],
    }

    generated_files: List[str] = []
    manifest_path = out_dir / "00_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    generated_files.append(manifest_path.name)

    layer_path = out_dir / "01_layers_summary.csv"
    write_csv(layer_path, ["file_name", "role", "size_bytes", "line_count", "g_cmd_count", "d_code_count", "coord_count", "aperture_count"], layer_rows)
    generated_files.append(layer_path.name)

    generated_files.extend(export_flying_probe_tables(flying_probe, out_dir))

    readme_path = out_dir / "05_readme.txt"
    readme_lines = [
        "gerber analyse bundle",
        f"module {module_slug}",
        f"source {gerber_zip}",
        "files",
        "00_manifest.json metadata index and file roles",
        "01_layers_summary.csv layer-level quick stats",
        "02_components.csv component table from flying probe",
        "03_pins.csv pin net coordinate table from flying probe",
        "04_nets_summary.csv net-level pin counts and bbox",
        "recommended formats",
        "json for metadata and stable machine parsing",
        "csv for fast tabular parsing by agent",
        "txt for concise human notes",
    ]
    readme_path.write_text("\n".join(readme_lines) + "\n", encoding="utf-8")
    generated_files.append(readme_path.name)

    metrics["gerber_analysis_dir"] = str(out_dir)
    metrics["gerber_analysis_files"] = generated_files

    add_finding(findings, "info", "Gerber analysis bundle generated", f"Generated {len(generated_files)} files in {out_dir}", "Use bundle files for fast agent parsing and follow-up checks", str(out_dir))
    return {"analysis_dir": out_dir, "flying_probe_data": flying_probe}


def is_mains_or_hv_net(net: str) -> bool:
    n = net.upper()
    hv_tokens = (
        "AC_", "HV_", "MAINS", "LINE", "LIVE", "NEUTRAL", "PHASE",
        "L_IN", "L_OUT", "N_IN", "N_OUT", "VAC", "VIN_HV"
    )
    if any(tok in n for tok in hv_tokens):
        return True
    # Numeric HV-like rails such as +24V +48V +110V
    m = re.search(r"([+-]?\d+(\.\d+)?)V", n)
    if m:
        try:
            v = float(m.group(1))
            if abs(v) >= 24.0:
                return True
        except Exception:
            pass
    return False


def is_logic_or_analog_lv_net(net: str) -> bool:
    n = net.upper()
    lv_tokens = (
        "GND", "AGND", "DGND", "ISO_GND", "VREF", "ANALOG", "DIGITAL", "CTRL",
        "I2C", "SPI", "UART", "CAN", "USB", "ETH", "VCC", "VDD", "3V3", "5V", "1V8", "1V2"
    )
    if any(tok in n for tok in lv_tokens):
        return True
    m = re.search(r"([+-]?\d+(\.\d+)?)V", n)
    if m:
        try:
            v = float(m.group(1))
            if abs(v) <= 12.0:
                return True
        except Exception:
            pass
    return False


def estimate_clearance_from_flying_probe(data: dict, findings: List[dict], metrics: Dict[str, object]) -> None:
    pins = data.get("pins", {})
    fields = pins.get("fields", [])
    rows = pins.get("rows", [])
    if not fields or not rows:
        add_finding(findings, "medium", "No pin geometry in flying probe", "Cannot estimate spacing", "Export flying probe with pin data")
        return

    index = {k: i for i, k in enumerate(fields)}
    required = ["PIN_NAME", "PIN_X", "PIN_Y", "NET_NAME", "PAD_SIZEX", "PAD_SIZEY"]
    if not all(k in index for k in required):
        add_finding(findings, "medium", "Flying probe fields incomplete", "Missing required fields for spacing", "Regenerate flying probe export")
        return

    pads = []
    seen = set()
    for r in rows:
        net = str(r[index["NET_NAME"]]).strip()
        if not net:
            continue
        item = (str(r[index["PIN_NAME"]]), net, float(r[index["PIN_X"]]), float(r[index["PIN_Y"]]), float(r[index["PAD_SIZEX"]]), float(r[index["PAD_SIZEY"]]))
        if item in seen:
            continue
        seen.add(item)
        pads.append(item)

    hv = [p for p in pads if is_mains_or_hv_net(p[1])]
    lv = [p for p in pads if is_logic_or_analog_lv_net(p[1])]
    if not hv or not lv:
        add_finding(findings, "medium", "Spacing scope insufficient", "Could not identify both HV and LV pad groups", "Check net naming and exports")
        return

    best_edge = None
    best_pair = None
    for a in hv:
        for b in lv:
            if a[0].split("_", 1)[0] == b[0].split("_", 1)[0]:
                continue
            center = math.hypot(a[2] - b[2], a[3] - b[3])
            edge = center - 0.5 * max(a[4], a[5]) - 0.5 * max(b[4], b[5])
            if best_edge is None or edge < best_edge:
                best_edge = edge
                best_pair = (a, b)

    if best_edge is None or best_pair is None:
        add_finding(findings, "medium", "Spacing estimate unavailable", "No valid pad pair found", "Review flying probe pin records")
        return

    best_mm = best_edge * MM_PER_MIL
    metrics["min_hv_lv_edge_mil"] = round(best_edge, 2)
    metrics["min_hv_lv_edge_mm"] = round(best_mm, 3)
    detail = f"Estimated minimum HV-LV pad edge spacing: {best_edge:.2f} mil ({best_mm:.3f} mm), pair: {best_pair[0][0]}[{best_pair[0][1]}] <-> {best_pair[1][0]}[{best_pair[1][1]}]"
    if best_edge < 236:
        add_finding(findings, "high", "HV-LV spacing estimate", detail, "Increase creepage and clearance to >= 6 mm and verify routing-level clearance")
    elif best_edge < 315:
        add_finding(findings, "medium", "HV-LV spacing estimate", detail, "Prefer >= 8 mm margin for robust mains-related design")
    else:
        add_finding(findings, "low", "HV-LV spacing estimate", detail, "Keep current spacing and verify with final DRC")

def detect_zero_ohm_ground_bridges(resistor_ohm: Dict[str, float], pin_net_map: Dict[str, Dict[str, str]]) -> List[Tuple[str, str, str]]:
    bridges: List[Tuple[str, str, str]] = []
    for ref, ohm in resistor_ohm.items():
        if ohm > 1.0:
            continue
        pn = pin_net_map.get(ref, {})
        n1 = pn.get("1")
        n2 = pn.get("2")
        if n1 and n2 and n1 != n2:
            bridges.append((ref, n1, n2))
    return bridges


def check_ground_strategy(net_names: set, bridges: List[Tuple[str, str, str]], findings: List[dict], metrics: Dict[str, object]) -> None:
    ground_nets = sorted([n for n in net_names if "GND" in n.upper() or n.upper() in {"EARTH", "PE", "FG", "CHASSIS"}])
    analog_grounds = [n for n in ground_nets if "AGND" in n.upper() or "ANALOG" in n.upper()]
    digital_grounds = [n for n in ground_nets if "DGND" in n.upper() or n.upper() == "GND" or "DIG" in n.upper()]
    iso_grounds = [n for n in ground_nets if "ISO" in n.upper()]
    pe_aliases = {"PE", "EARTH", "FG", "CHASSIS_GND", "CHASSIS"}
    has_pe = any(n.upper() in pe_aliases for n in net_names)
    has_acn = any(n.upper() in {"AC_N", "N", "NEUTRAL"} for n in net_names)

    if not ground_nets:
        add_finding(findings, "high", "Ground domain naming incomplete", "No ground-like nets found in netlist", "Define at least one ground net explicitly")
        return

    bridge_pairs: Dict[Tuple[str, str], List[str]] = {}
    for ref, n1, n2 in bridges:
        key = tuple(sorted((n1, n2)))
        bridge_pairs.setdefault(key, []).append(ref)
    metrics["zero_ohm_bridges"] = {f"{k[0]}<->{k[1]}": v for k, v in sorted(bridge_pairs.items())}
    metrics["ground_domains"] = {
        "all": ground_nets,
        "analog": analog_grounds,
        "digital": digital_grounds,
        "iso": iso_grounds,
    }

    # Keep backward-compatible AGND/DGND style checks if both domains exist.
    agnd = analog_grounds[0] if analog_grounds else ""
    dgnd = digital_grounds[0] if digital_grounds else ""
    if agnd and dgnd:
        key = tuple(sorted((agnd, dgnd)))
        refs = bridge_pairs.get(key, [])
        if not refs:
            add_finding(
                findings,
                "info",
                "AGND and DGND kept separated",
                f"No 0R or net-tie bridge detected between {agnd} and {dgnd}",
                "Single-ground designs are acceptable. Add star-point only when cross-domain reference is required",
            )
        elif len(refs) > 1:
            add_finding(findings, "medium", "Multiple AGND-DGND bridges", f"Detected: {', '.join(refs)}", "Keep a single controlled star-point")
        else:
            add_finding(findings, "low", "AGND-DGND bridge identified", f"Bridge: {refs[0]}", "Keep bridge close to mixed-signal boundary")
    elif analog_grounds and not digital_grounds:
        add_finding(findings, "info", "Single analog ground domain", f"Detected analog ground only: {analog_grounds[0]}", "This is acceptable for pure analog boards")
    elif digital_grounds and not analog_grounds:
        add_finding(findings, "info", "Single digital ground domain", f"Detected digital/default ground only: {digital_grounds[0]}", "This is acceptable for pure digital boards")

    # Isolation checks should only apply when isolated domain exists.
    for iso in iso_grounds:
        for g in ground_nets:
            if g == iso:
                continue
            key = tuple(sorted((iso, g)))
            refs = bridge_pairs.get(key, [])
            if refs:
                sev = "high" if "AC_" not in g.upper() else "medium"
                add_finding(findings, sev, "Isolation boundary shorted", f"Detected direct 0R bridge between {iso} and {g}: {', '.join(refs)}", "Remove direct bridge unless intentional and safety-validated")

    if iso_grounds and has_acn:
        for iso in iso_grounds:
            key = tuple(sorted(("AC_N", iso)))
            refs = bridge_pairs.get(key, [])
            if refs:
                add_finding(findings, "low", "ISO_GND reference bridge identified", f"Bridge to AC_N: {', '.join(refs)}", "Verify this matches isolation strategy and safety rules")
            else:
                add_finding(
                    findings,
                    "info",
                    "ISO_GND floating (no AC_N bridge)",
                    "No explicit AC_N-ISO_GND bridge detected. Floating isolated ground can be intentional",
                    "If signal reference is required add a controlled bridge and verify safety and EMC",
                )

    if has_acn and not has_pe:
        add_finding(findings, "info", "Protective-earth net not detected", "No PE EARTH FG net found in this module netlist", "If chassis grounding is required define PE at system level")


def check_impedance_readiness(net_names: set, doc_texts: List[str], findings: List[dict], metrics: Dict[str, object]) -> None:
    pairs = [n[:-2] for n in net_names if n.endswith("_P") and (n[:-2] + "_N") in net_names]
    stackup_tokens = ["stackup", "dielectric", "dk", "er", "copper thickness", "line width", "impedance", "microstrip", "stripline"]
    corpus = "\n".join(doc_texts).lower()
    token_hits = sum(1 for t in stackup_tokens if t in corpus)
    has_stackup = token_hits >= 3
    metrics["diff_pair_count"] = len(pairs)
    metrics["stackup_keyword_hits"] = token_hits

    if pairs and not has_stackup:
        add_finding(findings, "medium", "Impedance input data incomplete", f"Detected {len(pairs)} differential pair base net(s) but no complete stackup metadata", "Provide stackup dielectric copper thickness and width spacing constraints")
    elif pairs and has_stackup:
        add_finding(findings, "low", "Impedance readiness acceptable", f"Differential pair bases: {', '.join(sorted(pairs)[:8])}", "Verify final targets with field solver and length report")
    else:
        add_finding(findings, "info", "No explicit differential pair nets detected", "No *_P *_N pair found in netlist", "Skip differential impedance checks or rename pair nets explicitly")


def safe_res(resistor_ohm: Dict[str, float], name: str) -> Optional[float]:
    return resistor_ohm.get(name)


def parse_net_nominal_voltage(net: str, args: argparse.Namespace) -> Optional[float]:
    n = net.upper().replace(" ", "")
    ground_keys = ("GND", "AGND", "DGND", "ISO_GND", "PGND", "SGND", "EARTH", "PE")
    if any(k in n for k in ground_keys):
        return 0.0

    # Common patterns: +5V, -12V, 3V3, 1V8, 12V0
    m = re.search(r"([+-]?\d+)(V)(\d+)?", n)
    if m:
        sign_num = m.group(1)
        frac = m.group(3)
        try:
            base = float(sign_num)
            if frac:
                if abs(base) < 100:
                    base = float(f"{int(base)}.{frac}")
            return base
        except Exception:
            pass

    # AC line rails use configured RMS max as conservative nominal.
    if any(k in n for k in ("AC_L", "LINE", "LIVE", "MAINS", "VAC")):
        return float(args.mains_rms_max)
    if any(k in n for k in ("AC_N", "NEUTRAL", "N_IN", "N_OUT")):
        return 0.0
    return None


def analyze_resistor_voltage_current_power(
    resistor_ohm: Dict[str, float],
    pin_net_map: Dict[str, Dict[str, str]],
    args: argparse.Namespace,
    findings: List[dict],
    metrics: Dict[str, object],
) -> None:
    rows: List[Dict[str, object]] = []
    total_resistor_power = 0.0
    known_count = 0

    for ref, r_ohm in resistor_ohm.items():
        pins = pin_net_map.get(ref, {})
        n1 = pins.get("1")
        n2 = pins.get("2")
        if not n1 or not n2 or r_ohm <= 0:
            continue
        v1 = parse_net_nominal_voltage(n1, args)
        v2 = parse_net_nominal_voltage(n2, args)
        if v1 is None or v2 is None:
            continue
        vdiff = abs(v1 - v2)
        i_a = vdiff / r_ohm
        p_w = (vdiff * vdiff) / r_ohm
        known_count += 1
        total_resistor_power += p_w
        rows.append(
            {
                "ref": ref,
                "net1": n1,
                "net2": n2,
                "r_ohm": r_ohm,
                "v_diff": vdiff,
                "i_a": i_a,
                "p_w": p_w,
            }
        )

    rows.sort(key=lambda x: float(x["p_w"]), reverse=True)
    top_rows = rows[:12]
    metrics["resistor_electrical"] = {
        "known_count": known_count,
        "total_resistor_power_w": round(total_resistor_power, 6),
        "top": [
            {
                "ref": r["ref"],
                "net1": r["net1"],
                "net2": r["net2"],
                "r_ohm": round(float(r["r_ohm"]), 4),
                "v_diff": round(float(r["v_diff"]), 4),
                "i_a": round(float(r["i_a"]), 6),
                "p_w": round(float(r["p_w"]), 6),
            }
            for r in top_rows
        ],
    }

    if known_count == 0:
        add_finding(
            findings,
            "medium",
            "Voltage current calculation incomplete",
            "No resistor branch has both-end nominal voltages resolvable from net names",
            "Use standard rail names like 3V3 5V 12V AGND DGND to improve automatic calculations",
        )
        return

    hot = [r for r in rows if float(r["p_w"]) >= 0.2]
    warm = [r for r in rows if 0.1 <= float(r["p_w"]) < 0.2]
    if hot:
        items = ", ".join(f"{r['ref']}({float(r['p_w']):.3f}W)" for r in hot[:8])
        add_finding(
            findings,
            "high",
            "Resistor power risk",
            f"Detected high resistor power dissipation {items}",
            "Increase resistor watt rating or split power across multiple resistors",
        )
    elif warm:
        items = ", ".join(f"{r['ref']}({float(r['p_w']):.3f}W)" for r in warm[:8])
        add_finding(
            findings,
            "medium",
            "Resistor thermal margin warning",
            f"Detected medium resistor power dissipation {items}",
            "Check footprint power rating and ambient temperature margin",
        )
    else:
        add_finding(
            findings,
            "low",
            "Resistor electrical load acceptable",
            f"Computed {known_count} resistor branches total dissipation {total_resistor_power:.4f} W",
            "Validate with measured rail voltages during bring-up",
        )


def detect_diff_pairs(net_names: Set[str]) -> List[Tuple[str, str, str]]:
    pairs: List[Tuple[str, str, str]] = []
    name_set = set(net_names)

    # Base + suffix style pairs.
    for n in name_set:
        for a, b in (("_P", "_N"), ("_PLUS", "_MINUS"), ("_DP", "_DN"), ("_TXP", "_TXN"), ("_RXP", "_RXN")):
            if n.endswith(a):
                base = n[: -len(a)]
                mate = base + b
                if mate in name_set:
                    pairs.append((base, n, mate))
        if n.endswith("+"):
            mate = n[:-1] + "-"
            if mate in name_set:
                pairs.append((n[:-1], n, mate))

    uniq: Dict[Tuple[str, str], Tuple[str, str, str]] = {}
    for base, p, n in pairs:
        key = tuple(sorted((p, n)))
        uniq[key] = (base, p, n)
    return list(uniq.values())


def check_voltage_chain(
    resistor_ohm: Dict[str, float],
    pin_net_map: Dict[str, Dict[str, str]],
    args: argparse.Namespace,
    findings: List[dict],
    metrics: Dict[str, object],
) -> None:
    analyze_resistor_voltage_current_power(resistor_ohm, pin_net_map, args, findings, metrics)


def check_current_chain(
    nets: Dict[str, List[str]],
    findings: List[dict],
    metrics: Dict[str, object],
) -> None:
    # Generic signal chain sanity based on net fanout and likely floating nets.
    def ignore_single_pin_net(net: str) -> bool:
        n = net.upper()
        if n.startswith(("NC", "N/C", "TEST", "TP", "PAD", "FID", "MECH")):
            return True
        ignore_tokens = ("GND", "AGND", "DGND", "ISO_GND", "VCC", "VDD", "VIN", "VOUT", "AC_", "EARTH", "PE", "CHASSIS")
        return any(t in n for t in ignore_tokens)

    def is_power_or_ground(net: str) -> bool:
        n = net.upper()
        tokens = ("GND", "AGND", "DGND", "ISO_GND", "VCC", "VDD", "VIN", "VOUT", "3V3", "5V", "12V", "AC_", "PE", "EARTH")
        return any(t in n for t in tokens)

    net_sizes = {n: len(pins) for n, pins in nets.items()}
    floating = [n for n, c in net_sizes.items() if c <= 1 and not ignore_single_pin_net(n)]
    oversize = [n for n, c in net_sizes.items() if c >= 25 and not is_power_or_ground(n)]
    metrics["signal_chain"] = {
        "net_count": len(nets),
        "floating_net_count": len(floating),
        "high_fanout_net_count": len(oversize),
        "floating_sample": floating[:20],
        "high_fanout_sample": oversize[:20],
    }
    if floating:
        add_finding(
            findings,
            "medium",
            "Potential floating nets detected",
            f"Found {len(floating)} single-pin nets possibly floating",
            "Review intentional test stubs and tie unused inputs to defined levels",
        )
    if oversize:
        add_finding(
            findings,
            "low",
            "High fanout nets detected",
            f"Found {len(oversize)} high fanout nets",
            "Check drive strength timing and buffering for heavy fanout nets",
        )
    if not floating and not oversize:
        add_finding(
            findings,
            "low",
            "Signal chain connectivity looks consistent",
            "No obvious floating or extreme fanout nets detected",
            "Proceed to timing and SI checks in PCB tool",
        )


def ref_prefix(ref: str) -> str:
    m = re.match(r"^[A-Za-z]+", ref.strip())
    return m.group(0).upper() if m else ""


def is_ground_net_name(net: str) -> bool:
    n = net.upper()
    return any(k in n for k in ("GND", "AGND", "DGND", "ISO_GND", "PGND", "SGND", "EARTH", "PE", "CHASSIS"))


def is_power_net_name(net: str) -> bool:
    n = net.upper().replace(" ", "")
    if is_ground_net_name(n):
        return False
    tokens = ("VCC", "VDD", "VREF", "VAA", "VEE", "AVDD", "DVDD", "3V3", "5V", "1V8", "1V2", "12V", "24V", "VBAT")
    if any(t in n for t in tokens):
        return True
    m = re.search(r"([+-]?\d+)(V)(\d+)?", n)
    if m:
        try:
            v = float(m.group(1))
            return 0.9 <= abs(v) <= 60.0
        except Exception:
            return False
    return False


def is_connector_ref(ref: str) -> bool:
    p = ref_prefix(ref)
    if p in {"J", "P", "CN", "CON", "X", "XT", "K"}:
        return True
    return p.startswith("CN") or p.startswith("CON")


def collect_net_refs(nets: Dict[str, List[str]]) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for net, pins in nets.items():
        refs: Set[str] = set()
        for p in pins:
            if "." not in p:
                continue
            refs.add(p.split(".", 1)[0].strip())
        if refs:
            out[net] = refs
    return out


def has_resistor_bias_on_net(net: str, pin_net_map: Dict[str, Dict[str, str]]) -> bool:
    for ref, pins in pin_net_map.items():
        if ref_prefix(ref) != "R":
            continue
        vals = [v for v in pins.values() if v]
        if len(vals) < 2 or net not in vals:
            continue
        for other in vals:
            if other == net:
                continue
            if is_ground_net_name(other) or is_power_net_name(other):
                return True
    return False


def has_pullup_on_net(net: str, pin_net_map: Dict[str, Dict[str, str]]) -> bool:
    for ref, pins in pin_net_map.items():
        if ref_prefix(ref) != "R":
            continue
        vals = [v for v in pins.values() if v]
        if len(vals) < 2 or net not in vals:
            continue
        for other in vals:
            if other == net:
                continue
            if is_power_net_name(other):
                return True
    return False


def collect_decoupling_caps(pin_net_map: Dict[str, Dict[str, str]]) -> Dict[str, int]:
    cap_by_net: Dict[str, int] = {}
    for ref, pins in pin_net_map.items():
        if ref_prefix(ref) != "C":
            continue
        nets = [n for n in pins.values() if n]
        if len(nets) < 2:
            continue
        has_gnd = any(is_ground_net_name(n) for n in nets)
        if not has_gnd:
            continue
        for n in nets:
            if is_ground_net_name(n):
                continue
            if is_power_net_name(n):
                cap_by_net[n] = cap_by_net.get(n, 0) + 1
    return cap_by_net


def check_schematic_reasonability(
    nets: Dict[str, List[str]],
    pin_net_map: Dict[str, Dict[str, str]],
    findings: List[dict],
    metrics: Dict[str, object],
) -> None:
    net_refs = collect_net_refs(nets)
    cap_by_net = collect_decoupling_caps(pin_net_map)

    # 1) Power decoupling heuristic
    no_decouple: List[str] = []
    weak_decouple: List[str] = []
    for net, refs in net_refs.items():
        if not is_power_net_name(net):
            continue
        u_refs = sorted(r for r in refs if ref_prefix(r).startswith("U"))
        if not u_refs:
            continue
        cap_cnt = int(cap_by_net.get(net, 0))
        if cap_cnt == 0:
            no_decouple.append(f"{net}(U={len(u_refs)})")
        elif cap_cnt * 2 < len(u_refs):
            weak_decouple.append(f"{net}(U={len(u_refs)},C={cap_cnt})")

    metrics["schematic_power_decoupling"] = {
        "nets_no_decouple": no_decouple[:20],
        "nets_weak_decouple": weak_decouple[:20],
    }
    if no_decouple:
        add_finding(
            findings,
            "medium",
            "Power decoupling missing on supply nets",
            f"Supply nets without decoupling capacitor to ground: {', '.join(no_decouple[:8])}",
            "Add local bypass capacitors close to IC supply pins and verify return path",
        )
    elif weak_decouple:
        add_finding(
            findings,
            "low",
            "Power decoupling density can improve",
            f"Supply nets with limited decoupling: {', '.join(weak_decouple[:8])}",
            "Increase local decoupling density for heavily shared rails",
        )

    # 2) Control net pull bias heuristic
    control_nets: List[str] = []
    for net, refs in net_refs.items():
        n = net.upper()
        if not re.search(r"(RST|RESET|NRST|EN|ENABLE|BOOT|PDN|SHDN)", n):
            continue
        if is_power_net_name(net) or is_ground_net_name(net):
            continue
        if not any(ref_prefix(r).startswith("U") for r in refs):
            continue
        if not has_resistor_bias_on_net(net, pin_net_map):
            control_nets.append(net)
    metrics["schematic_control_bias"] = {"nets_no_bias": control_nets[:20]}
    if control_nets:
        add_finding(
            findings,
            "low",
            "Control nets may lack pull bias",
            f"Control-like nets without obvious bias resistor: {', '.join(control_nets[:8])}",
            "Add or verify pull-up/pull-down on reset enable boot control lines",
        )

    # 3) I2C pull-up heuristic
    i2c_targets = [n for n in net_refs if re.search(r"\\b(SDA|SCL)\\b", n.upper())]
    i2c_missing: List[str] = []
    for net in i2c_targets:
        refs = net_refs.get(net, set())
        if not any(ref_prefix(r).startswith("U") for r in refs):
            continue
        if not has_pullup_on_net(net, pin_net_map):
            i2c_missing.append(net)
    metrics["schematic_i2c_pullup"] = {"nets_missing_pullup": i2c_missing[:20]}
    if i2c_missing:
        sev = "medium" if len(i2c_missing) >= 2 else "low"
        add_finding(
            findings,
            sev,
            "I2C pull-up may be missing",
            f"I2C-like nets without clear pull-up resistor: {', '.join(i2c_missing[:8])}",
            "Add external pull-up resistors or document internal pull-up source",
        )

    # 4) External interface protection heuristic
    weak_iface: List[str] = []
    for net, refs in net_refs.items():
        if is_power_net_name(net) or is_ground_net_name(net):
            continue
        has_conn = any(is_connector_ref(r) for r in refs)
        has_ic = any(ref_prefix(r).startswith("U") for r in refs)
        if not (has_conn and has_ic):
            continue
        has_protect = any(ref_prefix(r) in {"D", "TVS", "ESD", "MOV"} for r in refs)
        has_series = any(ref_prefix(r) in {"R", "FB", "L"} for r in refs)
        if not (has_protect or has_series):
            weak_iface.append(net)

    metrics["schematic_interface_protection"] = {"weak_nets": weak_iface[:20]}
    if len(weak_iface) >= 2:
        add_finding(
            findings,
            "low",
            "External interface protection may be weak",
            f"Connector-to-IC nets without obvious series/protection: {', '.join(weak_iface[:8])}",
            "Consider series damping resistor and ESD or surge protection for external-facing nets",
        )


def main() -> int:
    args = parse_args()
    log_dir: Optional[Path] = None
    if args.log_mode == "on":
        module_slug = sanitize_module_slug(args.module)
        log_root = args.output_dir if args.output_dir is not None else Path(__file__).resolve().parents[1] / "log"
        log_dir = log_root / module_slug
    module_dir = resolve_module_dir(args)
    report_dir = prefer_user_report_dir(module_dir)
    findings: List[dict] = []
    metrics: Dict[str, object] = {}

    if not args.netlist.exists():
        add_finding(findings, "high", "Netlist missing", f"File not found: {args.netlist}", "Provide valid netlist")
        stamp = now_stamp()
        tasklist, tasklog = write_outputs(log_dir, report_dir, args.module, stamp, args, findings, metrics)
        print(tasklist)
        if tasklog is not None:
            print(tasklog)
        return 1

    gerber_path = resolve_gerber_path(args, module_dir)
    if gerber_path is None or not gerber_path.exists():
        msg = (
            f"未找到Gerber压缩包 模块目录 {module_dir} "
            "请提供 --gerber 或将Gerber zip放在模块目录中"
        )
        print(msg)
        add_finding(
            findings,
            "high",
            "Gerber missing",
            msg,
            "Provide --gerber or place latest Gerber zip in module workspace",
            str(module_dir),
        )
        stamp = now_stamp()
        tasklist, tasklog = write_outputs(log_dir, report_dir, args.module, stamp, args, findings, metrics)
        print(tasklist)
        if tasklog is not None:
            print(tasklog)
        return 1
    args.gerber = gerber_path
    module_dir = gerber_path.parent
    report_dir = prefer_user_report_dir(module_dir)

    bom_path = resolve_bom_path(args, module_dir)
    if bom_path is not None and bom_path.exists():
        args.bom = bom_path
        metrics["bom_auto_detected"] = str(bom_path)
        add_finding(
            findings,
            "info",
            "Auto BOM detected",
            f"Use BOM file {bom_path}",
            "Cost and datasheet logic will read this BOM automatically",
            str(bom_path),
        )
    else:
        args.bom = None
        add_finding(
            findings,
            "info",
            "BOM auto-detection failed",
            f"No BOM file found in module workspace {module_dir}",
            "Put BOM csv/xlsx/xls into module workspace or pass --bom",
            str(module_dir),
        )

    netlist_text = read_text(args.netlist)
    ref_to_raw_value, resistor_ohm = parse_packages(netlist_text)
    nets = parse_nets(netlist_text)
    bom_comps = parse_bom_components(args.bom)
    pin_net_map = build_component_pin_net_map(nets)
    net_names = netset_from_mapping(nets)
    ds_dir = args.datasheet_dir if args.datasheet_dir else (module_dir / "datasheet")
    component_info_cache_path = ds_dir / "_component_info_cache.json"
    component_info_state = load_component_info_state(component_info_cache_path)
    metrics["component_info_cache"] = str(component_info_cache_path)
    metrics["bom_component_count"] = len(bom_comps)
    profile = detect_analysis_profile(args.analysis_profile)
    metrics["analysis_profile"] = profile
    profile_detail = f"Profile {profile} (auto alias)" if args.analysis_profile == "auto" else f"Profile {profile}"
    add_finding(findings, "info", "Analysis profile selected", profile_detail, "Use --analysis-profile generic to override auto mode")

    gerber_bundle = build_gerber_analysis_bundle(args.gerber, args.module, findings, metrics)
    fp_data = gerber_bundle.get("flying_probe_data") if gerber_bundle else None
    if fp_data is not None:
        estimate_clearance_from_flying_probe(fp_data, findings, metrics)
    else:
        add_finding(findings, "medium", "No flying probe geometry", "Cannot estimate HV-LV spacing from Gerber package", "Export and include FlyingProbeTesting.json in Gerber zip")

    bridges = detect_zero_ohm_ground_bridges(resistor_ohm, pin_net_map)
    check_ground_strategy(net_names, bridges, findings, metrics)

    doc_texts = [read_text(p) for p in args.doc if p.exists()]
    check_impedance_readiness(net_names, doc_texts, findings, metrics)

    check_voltage_chain(resistor_ohm, pin_net_map, args, findings, metrics)
    check_current_chain(nets, findings, metrics)
    check_schematic_reasonability(nets, pin_net_map, findings, metrics)

    if args.datasheet_auto == "on":
        if bom_comps:
            all_comps = dedup_components(bom_comps)
            add_finding(
                findings,
                "info",
                "Component info source",
                f"Use BOM components only ({len(all_comps)} deduped) for datasheet updates",
                "Only changed BOM components will trigger refresh by cache signature",
            )
        else:
            netlist_comps = collect_netlist_part_candidates(ref_to_raw_value)
            simple_netlist_comps = collect_netlist_simple_components(ref_to_raw_value)
            all_comps = dedup_components(netlist_comps + simple_netlist_comps)
        if all_comps:
            sync_datasheets(
                ds_dir,
                all_comps,
                findings,
                metrics,
                add_finding,
                component_info_state=component_info_state,
            )
        else:
            add_finding(
                findings,
                "info",
                "No component candidates for datasheet sync",
                "Cannot infer part numbers from current BOM/netlist content",
                "Provide BOM with LCSC code or MPN to enable datasheet auto-download",
            )

    handle_file_management(args, module_dir, findings, metrics)

    power_w = estimate_power(
        pin_net_map,
        metrics,
        nets=nets,
        ref_to_raw_value=ref_to_raw_value,
        bom_components=bom_comps,
        datasheet_dir=ds_dir,
    )
    add_finding(findings, "info", "Power estimate", f"Estimated board power is about {power_w:.3f} W without full load profile", "Use measured rail currents for final thermal and efficiency budget")

    cost_total, cost_msg = parse_bom_cost(
        args.bom,
        load_bom_rows,
        price_auto=args.price_auto,
        component_info_state=component_info_state,
    )
    if cost_total is None:
        add_finding(findings, "medium", "Cost calculation incomplete", cost_msg, "Provide BOM csv or xlsx with quantity and unit price for exact cost")
    else:
        metrics["bom_total_cost"] = round(cost_total, 4)
        add_finding(findings, "info", "Cost estimate", f"BOM total cost is about {cost_total:.4f}", "Recheck supplier prices before procurement")
        if cost_msg != "ok":
            add_finding(findings, "low", "Cost estimate partially covered", cost_msg, "补充缺失型号的LCSC编码或可用型号以提高成本覆盖率")
    metrics["cost_table"] = build_cost_table_data(
        args.bom,
        load_bom_rows,
        max_rows=16,
        price_auto=args.price_auto,
        component_info_state=component_info_state,
    )

    if bom_comps:
        bom_keys: Set[str] = set()
        for comp in bom_comps:
            key = str(comp.get("component_key", "")).strip() or component_identity_key(
                lcsc=str(comp.get("lcsc", "")),
                mpn=str(comp.get("mpn", "")),
                ref=str(comp.get("ref", "")),
                desc=str(comp.get("desc", "")),
            )
            if key:
                bom_keys.add(key)
        trim_component_state(component_info_state, bom_keys)
    save_component_info_state(component_info_cache_path, component_info_state)

    stamp = now_stamp()
    tasklist, tasklog = write_outputs(log_dir, report_dir, args.module, stamp, args, findings, metrics)
    print(tasklist)
    if tasklog is not None:
        print(tasklog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
