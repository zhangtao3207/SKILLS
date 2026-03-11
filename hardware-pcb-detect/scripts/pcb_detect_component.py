#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_LCSC_PRICE_CACHE: Dict[str, Tuple[Optional[float], str]] = {}
_LCSC_CODE_CACHE: Dict[str, str] = {}

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


def http_get_text(url: str, timeout: int = 20) -> Optional[str]:
    try:
        ssl_ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception:
        return None


def http_download_file(url: str, target: Path, timeout: int = 25) -> bool:
    try:
        ssl_ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            data = resp.read()
        if len(data) < 512:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return True
    except Exception:
        return False


def is_pdf_candidate(url: str) -> bool:
    u = url.lower()
    if ".pdf" not in u:
        return False
    deny = ("iso", "certificate", "quality-assurance", "rohs", "terms-and-conditions")
    return not any(k in u for k in deny)


def extract_pdf_urls_from_html(html: str) -> List[str]:
    raw = re.findall(r"https?://[^\"\'\s>]+\.pdf", html, flags=re.IGNORECASE)
    seen: Set[str] = set()
    out: List[str] = []
    for u in raw:
        if not is_pdf_candidate(u):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def ddg_search_links(query: str, timeout: int = 20) -> List[str]:
    url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    html = http_get_text(url, timeout=timeout)
    if not html:
        return []
    links = re.findall(r'href="([^"]*duckduckgo\.com/l/\?[^"]+)"', html, flags=re.IGNORECASE)
    out: List[str] = []
    for link in links:
        qs = urllib.parse.urlparse(link).query
        params = urllib.parse.parse_qs(qs)
        uddg = params.get("uddg", [])
        if not uddg:
            continue
        real = urllib.parse.unquote(uddg[0]).strip()
        if real and real not in out:
            out.append(real)
    return out


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


def sanitize_file_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    stem = stem.strip("._-")
    return stem or f"part_{int(time.time())}"



def split_refs(ref_text: str) -> List[str]:
    parts = [r.strip() for r in re.split(r"[,;/\s]+", ref_text or "") if r.strip()]
    return parts


def normalize_refs_text(ref_text: str) -> str:
    refs = sorted({r.upper() for r in split_refs(ref_text)})
    return ",".join(refs)


def component_identity_key(lcsc: str = "", mpn: str = "", ref: str = "", desc: str = "") -> str:
    code = normalize_lcsc_code(lcsc)
    if code:
        return f"LCSC:{code}"
    mpn_key = (mpn or "").strip().upper()
    if mpn_key:
        return f"MPN:{mpn_key}"
    ref_key = normalize_refs_text(ref)
    if ref_key:
        return f"REF:{ref_key}"
    desc_key = (desc or "").strip().upper()
    if desc_key:
        return f"DESC:{desc_key[:64]}"
    return ""


def component_signature(lcsc: str = "", mpn: str = "", ref: str = "", desc: str = "", datasheet_url: str = "", product_url: str = "") -> str:
    payload = {
        "lcsc": normalize_lcsc_code(lcsc),
        "mpn": (mpn or "").strip().upper(),
        "ref": normalize_refs_text(ref),
        "desc": (desc or "").strip().upper(),
        "datasheet_url": (datasheet_url or "").strip(),
        "product_url": (product_url or "").strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_json_dict(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(read_text(path))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def load_component_info_state(cache_path: Path) -> Dict[str, object]:
    state = load_json_dict(cache_path)
    if not state:
        state = {"version": 1, "components": {}}
    if not isinstance(state.get("components"), dict):
        state["components"] = {}
    return state


def save_component_info_state(cache_path: Path, state: Dict[str, object]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    cache_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_component_state_entry(state: Dict[str, object], key: str) -> Dict[str, object]:
    comps = state.setdefault("components", {})
    if not isinstance(comps, dict):
        state["components"] = {}
        comps = state["components"]
    row = comps.get(key)
    if not isinstance(row, dict):
        row = {}
        comps[key] = row
    return row


def trim_component_state(state: Dict[str, object], keep_keys: Set[str]) -> None:
    comps = state.get("components")
    if not isinstance(comps, dict):
        return
    for k in list(comps.keys()):
        if k not in keep_keys:
            comps.pop(k, None)


def is_simple_component(component: dict) -> bool:
    refs = split_refs(str(component.get("ref", "")))
    prefixes: Set[str] = set()
    for ref in refs:
        m = re.match(r"^[A-Za-z]+", ref)
        if m:
            prefixes.add(m.group(0).upper())
    if prefixes and prefixes.issubset({"R", "RN", "C", "L", "SW", "KEY", "BTN", "FB", "RV", "VR", "NTC", "PTC"}):
        return True

    desc = (str(component.get("desc", "")) + " " + str(component.get("mpn", ""))).lower()
    words = ("resistor", "capacitor", "inductor", "button", "switch", "电阻", "电容", "电感", "按键", "开关")
    return any(w in desc for w in words)


def build_datasheet_candidates(component: dict) -> List[str]:
    candidates: List[str] = []
    ds_url = str(component.get("datasheet_url", "")).strip()
    pd_url = str(component.get("product_url", "")).strip()
    lcsc = str(component.get("lcsc", "")).strip()
    mpn = str(component.get("mpn", "")).strip()

    if ds_url.lower().startswith("http"):
        candidates.append(ds_url)

    product_urls: List[str] = []
    if pd_url.lower().startswith("http"):
        product_urls.append(pd_url)
    if re.fullmatch(r"C\d{4,}", lcsc, flags=re.IGNORECASE):
        product_urls.append(f"https://www.lcsc.com/search?q={lcsc}")
    if mpn:
        product_urls.append(f"https://www.lcsc.com/search?q={urllib.parse.quote(mpn)}")

    for url in product_urls:
        html = http_get_text(url, timeout=20)
        if html:
            for pdf in extract_pdf_urls_from_html(html):
                if pdf not in candidates:
                    candidates.append(pdf)

    queries: List[str] = []
    if lcsc:
        queries.append(f"site:lcsc.com {lcsc} datasheet pdf")
    if mpn:
        queries.append(f"site:lcsc.com {mpn} datasheet pdf")
    for q in queries:
        for link in ddg_search_links(q, timeout=20):
            low = link.lower()
            if low.endswith(".pdf") and "lcsc" in low and is_pdf_candidate(link):
                if link not in candidates:
                    candidates.append(link)
            elif "lcsc.com/product-detail/" in low:
                html = http_get_text(link, timeout=20)
                if not html:
                    continue
                for pdf in extract_pdf_urls_from_html(html):
                    if pdf not in candidates:
                        candidates.append(pdf)

    return candidates


def sync_datasheets(
    datasheet_dir: Path,
    components: List[dict],
    findings: List[dict],
    metrics: Dict[str, object],
    add_finding_fn,
    component_info_state: Optional[Dict[str, object]] = None,
) -> None:
    datasheet_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    existed = 0
    failed = 0
    attempted = 0
    cached_skip = 0
    changed_count = 0
    detail_rows: List[dict] = []
    simple_rows: List[dict] = []

    if component_info_state is None:
        component_info_state = {"version": 1, "components": {}}

    download_components: List[dict] = []
    for comp in components:
        if is_simple_component(comp):
            simple_rows.append(
                {
                    "source": comp.get("source", ""),
                    "ref": comp.get("ref", ""),
                    "part": comp.get("lcsc", "") or comp.get("mpn", ""),
                    "desc": comp.get("desc", ""),
                }
            )
        else:
            download_components.append(comp)

    for comp in download_components[:120]:
        lcsc = str(comp.get("lcsc", "")).strip()
        mpn = str(comp.get("mpn", "")).strip()
        desc = str(comp.get("desc", "")).strip()
        ref = str(comp.get("ref", "")).strip()
        durl = str(comp.get("datasheet_url", "")).strip()
        purl = str(comp.get("product_url", "")).strip()
        key = str(comp.get("component_key", "")).strip() or component_identity_key(lcsc=lcsc, mpn=mpn, ref=ref, desc=desc)
        if not key:
            continue
        sig = str(comp.get("component_sig", "")).strip() or component_signature(
            lcsc=lcsc,
            mpn=mpn,
            ref=ref,
            desc=desc,
            datasheet_url=durl,
            product_url=purl,
        )
        state_row = get_component_state_entry(component_info_state, key)
        old_sig = str(state_row.get("signature", ""))
        ds_state = state_row.get("datasheet", {})
        if not isinstance(ds_state, dict):
            ds_state = {}

        if old_sig == sig:
            prev_status = str(ds_state.get("status", ""))
            prev_file = str(ds_state.get("file", ""))
            if prev_status in ("downloaded", "exists") and prev_file:
                p = Path(prev_file)
                if p.exists() and p.stat().st_size > 2048:
                    cached_skip += 1
                    existed += 1
                    detail_rows.append({"part": key, "status": "exists_cached", "file": prev_file})
                    continue
            if prev_status == "missing":
                cached_skip += 1
                failed += 1
                detail_rows.append({"part": key, "status": "missing_cached", "note": str(ds_state.get("note", ""))})
                continue

        changed_count += 1
        stem = sanitize_file_stem(key)
        target = datasheet_dir / f"{stem}.pdf"
        if target.exists() and target.stat().st_size > 2048:
            existed += 1
            detail_rows.append({"part": key, "status": "exists", "file": str(target)})
            state_row["signature"] = sig
            state_row["datasheet"] = {"status": "exists", "file": str(target)}
            continue

        attempted += 1
        ok = False
        candidates = build_datasheet_candidates(comp)
        for url in candidates[:8]:
            if http_download_file(url, target):
                ok = True
                downloaded += 1
                detail_rows.append({"part": key, "status": "downloaded", "url": url, "file": str(target)})
                state_row["signature"] = sig
                state_row["datasheet"] = {"status": "downloaded", "url": url, "file": str(target)}
                break
        if not ok:
            failed += 1
            note = "no valid datasheet link found"
            detail_rows.append({"part": key, "status": "missing", "note": note})
            state_row["signature"] = sig
            state_row["datasheet"] = {"status": "missing", "note": note}

    others_path = datasheet_dir / "others.txt"
    other_lines: List[str] = ["简单元器件参数记录", "以下器件按规则不下载datasheet 仅记录参数", ""]
    for i, row in enumerate(simple_rows, 1):
        other_lines.append(f"{i} 位号 {row['ref']} 型号参数 {row['part']} 说明 {row['desc']}")
    others_path.write_text("\n".join(other_lines) + "\n", encoding="utf-8-sig")

    metrics["datasheet_sync"] = {
        "datasheet_dir": str(datasheet_dir),
        "components_total": len(components),
        "simple_logged": len(simple_rows),
        "download_targets": len(download_components),
        "changed_targets": changed_count,
        "cached_skip": cached_skip,
        "attempted": attempted,
        "downloaded": downloaded,
        "exists": existed,
        "missing": failed,
    }
    detail_path = datasheet_dir / "_datasheet_sync_report.json"
    detail_path.write_text(json.dumps(detail_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if failed > 0 and changed_count > 0:
        add_finding_fn(
            findings,
            "medium",
            "Datasheet auto-download partially incomplete",
            f"Downloaded {downloaded}, existed {existed}, missing {failed}. See {detail_path}",
            "Provide BOM with LCSC code or datasheet URL for unresolved parts",
            str(detail_path),
        )
    elif failed > 0 and changed_count == 0:
        add_finding_fn(
            findings,
            "low",
            "Datasheet sync reused cached results",
            f"No BOM component change detected; reused cached datasheet state, unresolved count {failed}",
            "Update BOM component model/LCSC code if you need to refresh unresolved datasheets",
            str(detail_path),
        )
    else:
        add_finding_fn(
            findings,
            "low",
            "Datasheet sync completed",
            f"Downloaded {downloaded}, existed {existed}, no missing parts, cached skip {cached_skip}",
            "Keep datasheet folder under version control for reproducibility",
            str(datasheet_dir),
        )
    add_finding_fn(
        findings,
        "info",
        "Simple components recorded to others",
        f"Recorded {len(simple_rows)} simple parts in {others_path}",
        "Use others.txt for resistor capacitor inductor switch parameter lookup",
        str(others_path),
    )



def parse_numeric_value(v: object) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"-?[0-9]+(?:\.[0-9]+)?", s.replace(",", ""))
    return float(m.group(0)) if m else None


def infer_qty_from_ref(ref_text: str) -> Optional[float]:
    refs = split_refs(ref_text)
    return float(len(refs)) if refs else None


def normalize_lcsc_code(raw: str) -> str:
    m = re.search(r"\b(C\d{4,})\b", (raw or "").upper())
    return m.group(1) if m else ""


def extract_lcsc_code_from_text(raw: str) -> str:
    if not raw:
        return ""
    direct = normalize_lcsc_code(raw)
    if direct:
        return direct
    m = re.search(r"/product-detail/(C\d{4,})\.html", raw, flags=re.IGNORECASE)
    return m.group(1).upper() if m else ""


def http_get_json(url: str, timeout: int = 20) -> Optional[dict]:
    text = http_get_text(url, timeout=timeout)
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def find_lcsc_code_by_keyword(keyword: str, allow_ddg: bool = True) -> str:
    key = keyword.strip().upper()
    if not key:
        return ""
    if key in _LCSC_CODE_CACHE:
        return _LCSC_CODE_CACHE[key]

    direct = normalize_lcsc_code(key)
    if direct:
        _LCSC_CODE_CACHE[key] = direct
        return direct

    pre_url = "https://wmsc.lcsc.com/ftps/wm/search/pre?keyword=" + urllib.parse.quote(keyword.strip())
    data = http_get_json(pre_url, timeout=16)
    if data and data.get("code") == 200:
        result = data.get("result")
        if isinstance(result, dict):
            for val in result.values():
                if isinstance(val, list):
                    for item in val:
                        code = extract_lcsc_code_from_text(str(item))
                        if code:
                            _LCSC_CODE_CACHE[key] = code
                            return code
                elif isinstance(val, str):
                    code = extract_lcsc_code_from_text(val)
                    if code:
                        _LCSC_CODE_CACHE[key] = code
                        return code

    if allow_ddg:
        query = f"site:lcsc.com/product-detail {keyword.strip()}"
        for link in ddg_search_links(query, timeout=16)[:10]:
            if "lcsc.com" not in link.lower():
                continue
            code = extract_lcsc_code_from_text(link)
            if code:
                _LCSC_CODE_CACHE[key] = code
                return code

    _LCSC_CODE_CACHE[key] = ""
    return ""


def fetch_lcsc_unit_price(lcsc_code: str) -> Tuple[Optional[float], str]:
    code = normalize_lcsc_code(lcsc_code)
    if not code:
        return None, "invalid_lcsc_code"
    if code in _LCSC_PRICE_CACHE:
        return _LCSC_PRICE_CACHE[code]

    detail_url = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=" + urllib.parse.quote(code)
    data = http_get_json(detail_url, timeout=20)
    if not data or data.get("code") != 200:
        _LCSC_PRICE_CACHE[code] = (None, "lcsc_api_error")
        return _LCSC_PRICE_CACHE[code]
    result = data.get("result")
    if not isinstance(result, dict):
        _LCSC_PRICE_CACHE[code] = (None, "lcsc_part_not_found")
        return _LCSC_PRICE_CACHE[code]

    prices: List[Tuple[float, float]] = []
    plist = result.get("productPriceList")
    if isinstance(plist, list):
        for it in plist:
            if not isinstance(it, dict):
                continue
            ladder = parse_numeric_value(it.get("ladder"))
            price = parse_numeric_value(it.get("usdPrice"))
            if price is None:
                price = parse_numeric_value(it.get("currencyPrice"))
            if price is None:
                price = parse_numeric_value(it.get("productPrice"))
            if price is None or price <= 0:
                continue
            prices.append((ladder if ladder is not None else float("inf"), float(price)))

    if prices:
        prices.sort(key=lambda x: x[0])
        out = (prices[0][1], f"auto:lcsc_api:{code}")
        _LCSC_PRICE_CACHE[code] = out
        return out

    for field in ("usdPrice", "currencyPrice", "productPrice"):
        pv = parse_numeric_value(result.get(field))
        if pv is not None and pv > 0:
            out = (pv, f"auto:lcsc_api:{code}")
            _LCSC_PRICE_CACHE[code] = out
            return out

    _LCSC_PRICE_CACHE[code] = (None, "lcsc_price_not_found")
    return _LCSC_PRICE_CACHE[code]


def analyze_bom_cost_data(
    bom_path: Optional[Path],
    load_bom_rows_fn,
    price_auto: str = "on",
    component_info_state: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    rows, status = load_bom_rows_fn(bom_path)
    if not rows:
        return {"status": status, "rows": [], "headers": ["位号", "型号", "数量", "单价", "小计"], "total": None}

    if component_info_state is None:
        component_info_state = {"version": 1, "components": {}}

    cols = {c.lower(): c for c in rows[0].keys()}

    def pick_col(cands: List[str]) -> Optional[str]:
        for cand in cands:
            for k, orig in cols.items():
                if cand in k:
                    return orig
        return None

    col_ref = pick_col(["designator", "reference", "refdes", "ref", "位号"])
    col_mpn = pick_col(["manufacturer part", "mpn", "part number", "model", "型号"])
    col_desc = pick_col(["description", "comment", "desc", "value"])
    col_lcsc = pick_col(["supplier part", "lcsc part", "lcsc part number", "lcsc", "jlcpcb part"])
    col_url = pick_col(["product page", "url", "link", "商品链接"])
    col_datasheet = pick_col(["datasheet", "data sheet", "规格书"])
    col_qty = pick_col(["qty", "quantity", "count", "数量"])
    col_unit = pick_col(
        [
            "unit price", "unit_price", "single price", "单价", "price/pcs", "pcs price",
            "lcsc price", "jlcpcb price", "supplier price",
        ]
    )
    col_total = pick_col(["line total", "extended", "total", "amount", "金额", "小计", "总价", "合计"])
    ambiguous_price_col = pick_col(["price"])

    entries: List[Dict[str, object]] = []
    auto_query = {
        "enabled": price_auto == "on",
        "attempted": 0,
        "resolved": 0,
        "failed": 0,
        "cached_hit": 0,
        "cached_miss": 0,
        "resolved_lcsc_code": 0,
        "ddg_queries": 0,
    }

    for row in rows:
        ref = str(row.get(col_ref, "")).strip() if col_ref else ""
        mpn = str(row.get(col_mpn, "")).strip() if col_mpn else ""
        desc = str(row.get(col_desc, "")).strip() if col_desc else ""
        qty_v = parse_numeric_value(row.get(col_qty, "")) if col_qty else None
        if qty_v is None or qty_v <= 0:
            qty_v = infer_qty_from_ref(ref)
        unit_v = parse_numeric_value(row.get(col_unit, "")) if col_unit else None
        total_v = parse_numeric_value(row.get(col_total, "")) if col_total else None
        price_source = "bom"

        lcsc_code = normalize_lcsc_code(str(row.get(col_lcsc, ""))) if col_lcsc else ""
        if not lcsc_code:
            for c in (col_url, col_datasheet, col_desc, col_mpn):
                if not c:
                    continue
                lcsc_code = extract_lcsc_code_from_text(str(row.get(c, "")))
                if lcsc_code:
                    break

        if total_v is None and qty_v is not None and unit_v is not None:
            total_v = qty_v * unit_v

        if price_auto == "on" and unit_v is None and total_v is None:
            auto_query["attempted"] += 1
            comp_key = component_identity_key(lcsc=lcsc_code, mpn=mpn, ref=ref, desc=desc)
            comp_sig = component_signature(
                lcsc=lcsc_code,
                mpn=mpn,
                ref=ref,
                desc=desc,
                datasheet_url=str(row.get(col_datasheet, "")) if col_datasheet else "",
                product_url=str(row.get(col_url, "")) if col_url else "",
            )
            state_row = get_component_state_entry(component_info_state, comp_key) if comp_key else {}
            cached_used = False
            if comp_key and state_row and str(state_row.get("signature", "")) == comp_sig:
                p_state = state_row.get("price", {})
                if isinstance(p_state, dict):
                    p_status = str(p_state.get("status", ""))
                    if p_status == "ok":
                        p_cached = parse_numeric_value(p_state.get("unit"))
                        if p_cached is not None:
                            unit_v = p_cached
                            price_source = f"cache:{p_state.get('source', 'auto')}"
                            auto_query["cached_hit"] += 1
                            cached_used = True
                    elif p_status == "missing":
                        auto_query["cached_miss"] += 1
                        cached_used = True

            if not cached_used:
                code_for_lookup = lcsc_code
                if not code_for_lookup:
                    kw = mpn or desc
                    if kw:
                        allow_ddg = auto_query["ddg_queries"] < 3
                        if allow_ddg:
                            auto_query["ddg_queries"] += 1
                        code_for_lookup = find_lcsc_code_by_keyword(kw, allow_ddg=allow_ddg)
                        if code_for_lookup:
                            auto_query["resolved_lcsc_code"] += 1
                if code_for_lookup:
                    p, src = fetch_lcsc_unit_price(code_for_lookup)
                    if p is not None:
                        unit_v = p
                        price_source = src
                        auto_query["resolved"] += 1
                        if not lcsc_code:
                            lcsc_code = code_for_lookup
                        if comp_key:
                            state_row["signature"] = comp_sig
                            state_row["price"] = {"status": "ok", "unit": p, "source": src}
                    else:
                        auto_query["failed"] += 1
                        if comp_key:
                            state_row["signature"] = comp_sig
                            state_row["price"] = {"status": "missing", "source": src}
                else:
                    auto_query["failed"] += 1
                    if comp_key:
                        state_row["signature"] = comp_sig
                        state_row["price"] = {"status": "missing", "source": "no_lcsc_code"}

        if total_v is None and qty_v is not None and unit_v is not None:
            total_v = qty_v * unit_v

        if not (ref or mpn or desc or lcsc_code or qty_v is not None or unit_v is not None or total_v is not None):
            continue
        entries.append(
            {
                "ref": ref,
                "mpn": mpn or desc,
                "qty": qty_v,
                "unit": unit_v,
                "line_total": total_v,
                "lcsc_code": lcsc_code,
                "price_source": price_source,
            }
        )

    entries.sort(key=lambda x: (x["line_total"] is None, -(x["line_total"] or 0.0)))

    total_sum = 0.0
    priced_rows = 0
    for e in entries:
        if e["line_total"] is not None:
            total_sum += float(e["line_total"])
            priced_rows += 1

    if not entries:
        out = {
            "status": "No usable BOM rows detected",
            "headers": ["位号", "型号", "数量", "单价", "小计"],
            "rows": [],
            "total": None,
            "source_rows": 0,
            "priced_rows": 0,
            "auto_query": auto_query,
        }
        return out

    if priced_rows == 0:
        if ambiguous_price_col and not col_unit and not col_total:
            status_text = "Ambiguous BOM price column detected use explicit unit price or line total header"
        else:
            status_text = "No usable BOM price columns detected"
        out = {
            "status": status_text,
            "headers": ["位号", "型号", "数量", "单价", "小计"],
            "rows": entries,
            "total": None,
            "source_rows": len(entries),
            "priced_rows": 0,
            "auto_query": auto_query,
        }
        return out

    out = {
        "status": "ok",
        "headers": ["位号", "型号", "数量", "单价", "小计"],
        "rows": entries,
        "total": round(total_sum, 4),
        "source_rows": len(entries),
        "priced_rows": priced_rows,
        "auto_query": auto_query,
    }
    return out


def parse_bom_cost(
    bom_path: Optional[Path],
    load_bom_rows_fn,
    price_auto: str = "on",
    component_info_state: Optional[Dict[str, object]] = None,
) -> Tuple[Optional[float], str]:
    info = analyze_bom_cost_data(
        bom_path,
        load_bom_rows_fn,
        price_auto=price_auto,
        component_info_state=component_info_state,
    )
    total = info.get("total")
    if not isinstance(total, (int, float)):
        return None, str(info.get("status", "No usable BOM price columns detected"))
    source_rows = int(info.get("source_rows", 0) or 0)
    priced_rows = int(info.get("priced_rows", 0) or 0)
    if source_rows > 0 and priced_rows < source_rows:
        return float(total), f"Partial BOM cost coverage {priced_rows}/{source_rows} rows"
    return float(total), str(info.get("status", "ok"))


def build_cost_table_data(
    bom_path: Optional[Path],
    load_bom_rows_fn,
    max_rows: int = 12,
    price_auto: str = "on",
    component_info_state: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    info = analyze_bom_cost_data(
        bom_path,
        load_bom_rows_fn,
        price_auto=price_auto,
        component_info_state=component_info_state,
    )
    rows = info.get("rows", [])
    show = rows[:max_rows] if isinstance(rows, list) else []
    return {
        "status": info.get("status", "未知"),
        "headers": info.get("headers", ["位号", "型号", "数量", "单价", "小计"]),
        "rows": show,
        "total": info.get("total"),
        "source_rows": info.get("source_rows", len(rows) if isinstance(rows, list) else 0),
        "priced_rows": info.get("priced_rows", 0),
        "shown_rows": len(show),
        "auto_query": info.get("auto_query", {}),
    }
