#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import shutil
import subprocess
import csv
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

def display_width(s: str) -> int:
    w = 0
    for ch in s:
        ea = unicodedata.east_asian_width(ch)
        w += 2 if ea in ("W", "F") else 1
    return w


def pad_display(s: str, width: int, right: bool = False) -> str:
    pad = max(width - display_width(s), 0)
    return (" " * pad + s) if right else (s + " " * pad)


def format_row(values: List[str], widths: List[int], right_cols: Set[int]) -> str:
    cells = []
    for i, v in enumerate(values):
        cells.append(pad_display(v, widths[i], right=(i in right_cols)))
    return "| " + " | ".join(cells) + " |"


def append_ascii_table(
    lines_task: List[str],
    headers: List[str],
    rows: List[List[str]],
    right_cols: Optional[Set[int]] = None,
    empty_text: str = "无",
) -> None:
    right = right_cols or set()
    widths: List[int] = []
    for i, h in enumerate(headers):
        col_vals = [h] + [r[i] if i < len(r) else "" for r in rows]
        widths.append(max(display_width(v) for v in col_vals))

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines_task.append(sep)
    lines_task.append(format_row(headers, widths, right_cols=set()))
    lines_task.append(sep)
    if not rows:
        lines_task.append(empty_text)
    else:
        for vals in rows:
            norm = vals + [""] * (len(headers) - len(vals))
            lines_task.append(format_row(norm[: len(headers)], widths, right_cols=right))
    lines_task.append(sep)


def append_cost_table(lines_task: List[str], metrics: Dict[str, object], bom_path: Optional[Path]) -> None:
    lines_task.append("")
    lines_task.append("#成本计算表")
    lines_task.append(f"BOM文件 {bom_path if bom_path else '未提供'}")
    table = metrics.get("cost_table", {})
    if not isinstance(table, dict) or table.get("status") != "ok":
        status_txt = table.get("status", "未知") if isinstance(table, dict) else "未知"
        lines_task.append(f"状态 {to_cn_text(str(status_txt))}")
        return

    headers = table.get("headers", ["位号", "型号", "数量", "单价", "小计"])
    rows = table.get("rows", [])
    str_rows: List[List[str]] = []
    for r in rows:
        qty = "" if r.get("qty") is None else f"{r.get('qty'):.4g}"
        unit = "" if r.get("unit") is None else f"{r.get('unit'):.4f}"
        line_total = "" if r.get("line_total") is None else f"{r.get('line_total'):.4f}"
        str_rows.append([str(r.get("ref", "")), str(r.get("mpn", "")), qty, unit, line_total])

    append_ascii_table(
        lines_task,
        [str(h) for h in headers],
        str_rows,
        right_cols={2, 3, 4},
    )
    total = table.get("total")
    lines_task.append(f"合计 {total if total is not None else '无法计算'}")
    auto_q = table.get("auto_query", {})
    if isinstance(auto_q, dict) and auto_q.get("enabled"):
        tried = int(auto_q.get("attempted", 0) or 0)
        resolved = int(auto_q.get("resolved", 0) or 0)
        failed = int(auto_q.get("failed", 0) or 0)
        mapped = int(auto_q.get("resolved_lcsc_code", 0) or 0)
        cache_hit = int(auto_q.get("cached_hit", 0) or 0)
        cache_miss = int(auto_q.get("cached_miss", 0) or 0)
        if tried > 0:
            lines_task.append(f"自动询价 尝试 {tried} 成功 {resolved} 失败 {failed} 缓存命中 {cache_hit} 缓存缺失 {cache_miss} 型号映射LCSC编码 {mapped}")


def append_power_analysis(lines_task: List[str], metrics: Dict[str, object]) -> None:
    lines_task.append("")
    lines_task.append("#功耗分析")

    total = metrics.get("estimated_power_w")
    model = metrics.get("power_model", {}) if isinstance(metrics.get("power_model"), dict) else {}
    ds = model.get("datasheet_match", {}) if isinstance(model.get("datasheet_match"), dict) else {}
    matched = int(ds.get("matched_components", 0) or 0)
    scanned = int(ds.get("scanned_pdf_count", 0) or 0)
    stage_count = int(model.get("stage_count", 0) or 0)
    topo_text = str(model.get("topology_summary", "未识别到可计算的电源拓扑"))

    rows = [
        ["估算输入功率", f"{total:.6f} W" if isinstance(total, (int, float)) else "未知", "按拓扑级联反推"],
        ["估算输出负载", f"{float(model.get('total_output_load_w', 0.0)):.6f} W", "含下游电源轨与外部负载假设"],
        ["估算转换损耗", f"{float(model.get('total_conversion_loss_w', 0.0)):.6f} W", "含效率/静态电流与布局惩罚"],
        ["电源级数", str(stage_count), topo_text],
        ["数据手册覆盖", f"已匹配 {matched} 个器件 / 扫描 {scanned} 份PDF", "缺失参数使用保守默认值"],
        ["结论", "仅供工程估算", "建议结合实测电流与热数据校核"],
    ]
    headers = ["项目", "结果", "说明"]
    append_ascii_table(lines_task, headers, rows, right_cols={1})

    stage_rows = model.get("stage_breakdown", [])
    if isinstance(stage_rows, list) and stage_rows:
        lines_task.append("")
        lines_task.append("#功耗分解")
        lines_task.append("格式 位号 拓扑 Vin->Vout Iout(A) Pout(W) Ploss(W) 效率(%) 参数来源")
        for idx, item in enumerate(stage_rows[:12], 1):
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref", ""))
            topo = str(item.get("type", ""))
            vin = str(item.get("vin_net", ""))
            vout = str(item.get("vout_net", ""))
            iout = float(item.get("i_out_a", 0.0) or 0.0)
            pout = float(item.get("p_out_w", 0.0) or 0.0)
            ploss = float(item.get("p_loss_w", 0.0) or 0.0)
            eff = float(item.get("eff_pct", 0.0) or 0.0)
            src = str(item.get("param_source", "default"))
            lines_task.append(
                f"{idx} {ref} {topo} {vin}->{vout} {iout:.4f} {pout:.4f} {ploss:.4f} {eff:.2f} {src}"
            )


def append_netlist_layout_advice(lines_task: List[str], metrics: Dict[str, object]) -> None:
    def read_list(value: object, limit: int = 6) -> List[str]:
        if not isinstance(value, list):
            return []
        out: List[str] = []
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            out.append(text)
            if len(out) >= limit:
                break
        return out

    def join_items(items: List[str]) -> str:
        return "、".join(items)

    suggestions: List[str] = []
    cautions: List[str] = []

    decouple = metrics.get("schematic_power_decoupling", {})
    no_decouple: List[str] = []
    weak_decouple: List[str] = []
    if isinstance(decouple, dict):
        no_decouple = read_list(decouple.get("nets_no_decouple"))
        weak_decouple = read_list(decouple.get("nets_weak_decouple"))
    if no_decouple:
        suggestions.append(
            f"供电网络({join_items(no_decouple)})未识别到去耦，建议在对应芯片电源脚旁放置去耦电容并就近落地过孔，缩短回流回路。"
        )
    elif weak_decouple:
        suggestions.append(
            f"供电网络({join_items(weak_decouple)})去耦密度偏低，建议按负载分布补充本地去耦并避免跨区供电回流。"
        )

    control_bias = metrics.get("schematic_control_bias", {})
    control_nets: List[str] = []
    if isinstance(control_bias, dict):
        control_nets = read_list(control_bias.get("nets_no_bias"))
    if control_nets:
        cautions.append(
            f"控制网络({join_items(control_nets)})未见明确偏置，布局时应预留上拉/下拉器件并尽量靠近受控芯片。"
        )

    i2c_info = metrics.get("schematic_i2c_pullup", {})
    i2c_nets: List[str] = []
    if isinstance(i2c_info, dict):
        i2c_nets = read_list(i2c_info.get("nets_missing_pullup"))
    if i2c_nets:
        suggestions.append(
            f"I2C网络({join_items(i2c_nets)})建议优先在主控侧放置上拉电阻，保持SDA/SCL同层并减少长距离并行。"
        )

    iface_info = metrics.get("schematic_interface_protection", {})
    weak_iface: List[str] = []
    if isinstance(iface_info, dict):
        weak_iface = read_list(iface_info.get("weak_nets"))
    if weak_iface:
        cautions.append(
            f"外部接口网络({join_items(weak_iface)})未识别串联/防护器件，连接器入口建议优先布置TVS/ESD与阻尼元件。"
        )

    signal_info = metrics.get("signal_chain", {})
    floating_nets: List[str] = []
    high_fanout_nets: List[str] = []
    if isinstance(signal_info, dict):
        floating_nets = read_list(signal_info.get("floating_sample"))
        high_fanout_nets = read_list(signal_info.get("high_fanout_sample"))
    if high_fanout_nets:
        suggestions.append(
            f"高扇出网络({join_items(high_fanout_nets)})建议缩短主干并采用就近扇出，必要时增加缓冲器降低负载。"
        )
    if floating_nets:
        cautions.append(
            f"检测到疑似悬空网络({join_items(floating_nets)})，投板前需确认是否测试点/保留焊盘，避免不确定电平。"
        )

    ground_domains = metrics.get("ground_domains", {})
    analog_grounds: List[str] = []
    digital_grounds: List[str] = []
    iso_grounds: List[str] = []
    if isinstance(ground_domains, dict):
        analog_grounds = read_list(ground_domains.get("analog"), limit=3)
        digital_grounds = read_list(ground_domains.get("digital"), limit=3)
        iso_grounds = read_list(ground_domains.get("iso"), limit=3)

    zero_bridges = metrics.get("zero_ohm_bridges", {})
    bridge_count = len(zero_bridges) if isinstance(zero_bridges, dict) else 0
    if analog_grounds and digital_grounds:
        suggestions.append(
            f"检测到模拟地({join_items(analog_grounds)})与数字地({join_items(digital_grounds)})，建议分区布线并在单点受控桥接。"
        )
    if iso_grounds:
        cautions.append(
            f"检测到隔离地域({join_items(iso_grounds)})，请保持隔离带连续，避免跨隔离边界铺铜或并行跨越走线。"
        )
    if bridge_count >= 2:
        cautions.append("网表显示存在多个地桥接路径，需确认不会形成并联回流环路。")

    diff_pair_count = int(metrics.get("diff_pair_count", 0) or 0)
    stackup_hits = int(metrics.get("stackup_keyword_hits", 0) or 0)
    if diff_pair_count > 0:
        suggestions.append(
            f"网表检测到 {diff_pair_count} 组差分对命名，布局时应成对同层等长并保持连续参考平面。"
        )
        if stackup_hits < 3:
            cautions.append("差分阻抗输入信息不足，布线前应先确定层叠、线宽和线距目标。")

    lines_task.append("")
    lines_task.append("#基于网表的PCB布局建议与注意事项")
    lines_task.append("说明 以下内容由网表连通关系自动归纳，需结合PCB叠层和实测结果最终确认")
    lines_task.append("")
    lines_task.append("布局建议")
    if suggestions:
        for idx, item in enumerate(suggestions, 1):
            lines_task.append(f"{idx} {item}")
    else:
        lines_task.append("1 未识别到显著布局风险，建议保持当前分区并执行常规DRC与回流路径检查。")

    lines_task.append("")
    lines_task.append("注意事项")
    if cautions:
        for idx, item in enumerate(cautions, 1):
            lines_task.append(f"{idx} {item}")
    else:
        lines_task.append("1 当前网表未显示关键注意项，仍建议在投板前复核电源回流和接口防护。")

def append_error_analysis(lines_task: List[str], metrics: Dict[str, object]) -> None:
    lines_task.append("")
    lines_task.append("#误差分析")

    rows = [
        ["主要误差源", "器件容差 温漂 供电波动", "通用硬件误差项"],
        ["布局相关误差", "地回流 串扰 寄生参数", "需结合实测与仿真优化"],
        ["量化与采样误差", "由具体采样芯片与采样链决定", "脚本不输出器件绑定的固定步进值"],
        ["算法可补偿误差", "增益 零点 相位", "建议做多点校准与温度补偿"],
        ["结论", "具备工程可行性", "样机标定后输出最终误差指标"],
    ]

    headers = ["项目", "结果", "说明"]
    append_ascii_table(lines_task, headers, rows)


def estimate_power(
    pin_net_map: Dict[str, Dict[str, str]],
    metrics: Dict[str, object],
    nets: Optional[Dict[str, List[str]]] = None,
    ref_to_raw_value: Optional[Dict[str, str]] = None,
    bom_components: Optional[List[dict]] = None,
    datasheet_dir: Optional[Path] = None,
) -> float:
    def split_refs(ref_text: str) -> List[str]:
        if not ref_text:
            return []
        return [r.strip().upper() for r in re.split(r"[,;/\s]+", ref_text) if r.strip()]

    def ref_prefix(ref: str) -> str:
        m = re.match(r"^[A-Za-z]+", ref.strip())
        return m.group(0).upper() if m else ""

    def is_ground_net(net: str) -> bool:
        n = net.upper()
        return any(k in n for k in ("GND", "AGND", "DGND", "PGND", "ISO_GND", "EARTH", "PE", "CHASSIS"))

    def parse_rail_voltage(net: str) -> Optional[float]:
        if not net:
            return None
        n = net.upper().replace(" ", "")
        if is_ground_net(n):
            return 0.0

        m = re.search(r"([+-]?\d+)V(\d+)", n)
        if m:
            whole = float(m.group(1))
            frac = float(m.group(2)) / (10 ** len(m.group(2)))
            return whole + (frac if whole >= 0 else -frac)

        m = re.search(r"([+-]?\d+(?:\.\d+)?)V", n)
        if m:
            return float(m.group(1))

        aliases = {
            "VBUS": 5.0,
            "VCC": 5.0,
            "VDD": 3.3,
            "AVDD": 3.3,
            "DVDD": 3.3,
        }
        for k, v in aliases.items():
            if k in n:
                return v
        return None

    def is_ac_net(net: str) -> bool:
        n = net.upper()
        ac_tokens = ("AC_", "MAINS", "LINE", "LIVE", "NEUTRAL", "VAC", "L_IN", "N_IN")
        return any(t in n for t in ac_tokens)

    def normalize_token(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    def extract_efficiency(text: str) -> Optional[float]:
        if not text:
            return None
        candidates: List[float] = []
        for m in re.finditer(r"(?:efficien\w*)[^%\n\r]{0,40}?([0-9]{2,3}(?:\.[0-9]+)?)\s*%", text, flags=re.IGNORECASE):
            candidates.append(float(m.group(1)))
        for m in re.finditer(r"([0-9]{2,3}(?:\.[0-9]+)?)\s*%[^\n\r]{0,20}(?:efficien\w*)", text, flags=re.IGNORECASE):
            candidates.append(float(m.group(1)))
        candidates = [c for c in candidates if 40.0 <= c <= 100.0]
        if not candidates:
            return None
        return max(candidates) / 100.0

    def extract_keyword_current(text: str, keywords: List[str]) -> Optional[float]:
        if not text:
            return None
        pattern_kw = "|".join(re.escape(k) for k in keywords)
        candidates: List[float] = []
        for m in re.finditer(rf"(?:{pattern_kw})[^\n\r]{{0,50}}?([0-9]+(?:\.[0-9]+)?)\s*(uA|mA|A)", text, flags=re.IGNORECASE):
            val = float(m.group(1))
            unit = m.group(2).lower()
            if unit == "ma":
                val *= 1e-3
            elif unit == "ua":
                val *= 1e-6
            candidates.append(val)
        if not candidates:
            return None
        return min(candidates)

    def extract_keyword_voltage(text: str, keywords: List[str]) -> Optional[float]:
        if not text:
            return None
        pattern_kw = "|".join(re.escape(k) for k in keywords)
        vals: List[float] = []
        for m in re.finditer(rf"(?:{pattern_kw})[^\n\r]{{0,50}}?([0-9]+(?:\.[0-9]+)?)\s*(mV|V)", text, flags=re.IGNORECASE):
            v = float(m.group(1))
            if m.group(2).lower() == "mv":
                v *= 1e-3
            vals.append(v)
        if not vals:
            return None
        return min(vals)

    def extract_keyword_resistance(text: str, keywords: List[str]) -> Optional[float]:
        if not text:
            return None
        pattern_kw = "|".join(re.escape(k) for k in keywords)
        vals: List[float] = []
        for m in re.finditer(rf"(?:{pattern_kw})[^\n\r]{{0,50}}?([0-9]+(?:\.[0-9]+)?)\s*(mohm|ohm|mω|ω)", text, flags=re.IGNORECASE):
            r = float(m.group(1))
            unit = m.group(2).lower().replace("ω", "ohm")
            if "m" in unit and "ohm" in unit:
                r *= 1e-3
            vals.append(r)
        if not vals:
            return None
        return min(vals)

    def extract_keyword_power(text: str, keywords: List[str]) -> Optional[float]:
        if not text:
            return None
        pattern_kw = "|".join(re.escape(k) for k in keywords)
        vals: List[float] = []
        for m in re.finditer(rf"(?:{pattern_kw})[^\n\r]{{0,50}}?([0-9]+(?:\.[0-9]+)?)\s*(mW|W)", text, flags=re.IGNORECASE):
            p = float(m.group(1))
            if m.group(2).lower() == "mw":
                p *= 1e-3
            vals.append(p)
        if not vals:
            return None
        return max(vals)

    def read_pdf_text(pdf: Path, max_pages: int = 8) -> str:
        tool = shutil.which("pdftotext")
        if tool:
            try:
                proc = subprocess.run(
                    [tool, "-f", "1", "-l", str(max_pages), str(pdf), "-"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=40,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout
            except Exception:
                pass
        try:
            return pdf.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            try:
                return pdf.read_text(encoding="latin1", errors="ignore")
            except Exception:
                return ""

    def load_net_span_map(metrics_obj: Dict[str, object]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        d = metrics_obj.get("gerber_analysis_dir")
        if not d:
            return out
        csv_path = Path(str(d)) / "04_nets_summary.csv"
        if not csv_path.exists():
            return out
        try:
            with csv_path.open("r", encoding="utf-8", errors="ignore") as f:
                for row in csv.DictReader(f):
                    net = (row.get("net_name") or "").strip()
                    if not net:
                        continue
                    try:
                        min_x = float(row.get("min_x_mil") or 0.0)
                        max_x = float(row.get("max_x_mil") or 0.0)
                        min_y = float(row.get("min_y_mil") or 0.0)
                        max_y = float(row.get("max_y_mil") or 0.0)
                    except Exception:
                        continue
                    span = math.hypot(max_x - min_x, max_y - min_y)
                    out[net] = span
        except Exception:
            return {}
        return out

    refs = set(pin_net_map.keys())
    count_u = sum(1 for r in refs if r.upper().startswith("U"))
    count_q = sum(1 for r in refs if r.upper().startswith("Q"))
    count_diode = sum(1 for r in refs if r.upper().startswith(("D", "LED")))

    ref_catalog: Dict[str, Dict[str, str]] = {}
    for ref in refs:
        ref_catalog[ref.upper()] = {"ref": ref.upper(), "part": "", "lcsc": "", "desc": ""}

    if isinstance(ref_to_raw_value, dict):
        for ref, raw in ref_to_raw_value.items():
            r = ref.upper().strip()
            if r not in ref_catalog:
                ref_catalog[r] = {"ref": r, "part": "", "lcsc": "", "desc": ""}
            rv = str(raw or "").strip()
            if rv and rv != "'{Value}'" and rv != "{Value}":
                ref_catalog[r]["part"] = rv

    if isinstance(bom_components, list):
        for comp in bom_components:
            if not isinstance(comp, dict):
                continue
            mpn = str(comp.get("mpn", "")).strip()
            lcsc = str(comp.get("lcsc", "")).strip()
            desc = str(comp.get("desc", "")).strip()
            for ref in split_refs(str(comp.get("ref", ""))):
                if ref not in ref_catalog:
                    ref_catalog[ref] = {"ref": ref, "part": "", "lcsc": "", "desc": ""}
                if mpn:
                    ref_catalog[ref]["part"] = mpn
                if lcsc:
                    ref_catalog[ref]["lcsc"] = lcsc
                if desc:
                    ref_catalog[ref]["desc"] = desc

    datasheet_dirs: List[Path] = []
    if datasheet_dir is not None:
        datasheet_dirs.append(Path(datasheet_dir))
        alt = Path(datasheet_dir).parent.parent / "datasheet"
        if alt != Path(datasheet_dir):
            datasheet_dirs.append(alt)
    pdf_files: List[Path] = []
    for d in datasheet_dirs:
        if d.exists() and d.is_dir():
            pdf_files.extend(sorted(d.glob("*.pdf")))
    pdf_files = [p for p in pdf_files if p.exists() and p.is_file()]

    pdf_index = {p: normalize_token(p.stem) for p in pdf_files}
    pdf_text_cache: Dict[Path, str] = {}

    def find_component_text(ref: str, part: str, lcsc: str, desc: str) -> tuple[str, str]:
        matched: List[Path] = []
        if lcsc:
            tl = normalize_token(lcsc)
            for p, stem in pdf_index.items():
                if tl and tl in stem:
                    matched.append(p)

        if not matched and part:
            t_part = normalize_token(part)
            for p, stem in pdf_index.items():
                if t_part and (t_part in stem or stem in t_part):
                    matched.append(p)

        if not matched and ref:
            t_ref = normalize_token(ref)
            for p, stem in pdf_index.items():
                if t_ref and t_ref in stem:
                    matched.append(p)

        matched = matched[:2]
        txt_parts: List[str] = []
        for p in matched:
            if p not in pdf_text_cache:
                pdf_text_cache[p] = read_pdf_text(p)
            txt_parts.append(pdf_text_cache[p])

        merged = "\\n".join(txt_parts)
        bom_like = f"{part} {desc} {lcsc}"
        return (merged + "\\n" + bom_like).lower(), ",".join(x.name for x in matched)

    if nets is None:
        nets = {}

    ref_nets: Dict[str, Set[str]] = {}
    net_refs: Dict[str, Set[str]] = {}
    for net, pins in nets.items():
        for pin in pins:
            if "." not in pin:
                continue
            ref = pin.split(".", 1)[0].upper().strip()
            ref_nets.setdefault(ref, set()).add(net)
            net_refs.setdefault(net, set()).add(ref)

    stages: List[Dict[str, object]] = []
    used_u: Set[str] = set()

    for net in nets.keys():
        nu = net.upper()
        if not re.search(r"\b(SW|LX|PHASE)\b", nu):
            continue
        refs_sw = net_refs.get(net, set())
        u_refs = sorted([r for r in refs_sw if ref_prefix(r) == "U"])
        l_refs = sorted([r for r in refs_sw if ref_prefix(r) in {"L", "FB"}])
        if not u_refs or not l_refs:
            continue
        u_ref = u_refs[0]
        l_ref = l_refs[0]

        l_nets = list(ref_nets.get(l_ref, set()))
        out_candidates = [n for n in l_nets if n != net and not is_ground_net(n)]
        if not out_candidates:
            continue
        out_candidates.sort(key=lambda n: abs(parse_rail_voltage(n) or 0.0))
        vout_net = out_candidates[0]

        u_nets = [n for n in ref_nets.get(u_ref, set()) if n not in {net, vout_net} and not is_ground_net(n)]
        vin_net = ""
        best_v = -1.0
        for cand in u_nets:
            v = parse_rail_voltage(cand)
            if v is None:
                continue
            if v > best_v:
                best_v = v
                vin_net = cand
        if not vin_net and u_nets:
            vin_net = u_nets[0]

        d_ref = ""
        for r in refs_sw:
            if ref_prefix(r) == "D":
                d_ref = r
                break

        stages.append({
            "ref": u_ref,
            "type": "buck",
            "sw_net": net,
            "vin_net": vin_net,
            "vout_net": vout_net,
            "inductor_ref": l_ref,
            "diode_ref": d_ref,
        })
        used_u.add(u_ref)

    for ref, nset in ref_nets.items():
        if ref_prefix(ref) != "U" or ref in used_u:
            continue
        nlist = list(nset)
        if not nlist:
            continue
        has_ac = any(is_ac_net(n) for n in nlist)
        pwr_nets = [n for n in nlist if (parse_rail_voltage(n) or 0.0) > 0.0]

        if has_ac and pwr_nets:
            pwr_nets.sort(key=lambda n: parse_rail_voltage(n) or 0.0)
            stages.append({
                "ref": ref,
                "type": "acdc",
                "vin_net": "AC_MAINS",
                "vout_net": pwr_nets[-1],
                "sw_net": "",
                "inductor_ref": "",
                "diode_ref": "",
            })
            continue

        if len(pwr_nets) >= 2:
            pwr_nets.sort(key=lambda n: parse_rail_voltage(n) or 0.0, reverse=True)
            v_in = parse_rail_voltage(pwr_nets[0]) or 0.0
            v_out = parse_rail_voltage(pwr_nets[-1]) or 0.0
            if v_in > v_out > 0:
                stages.append({
                    "ref": ref,
                    "type": "linear",
                    "vin_net": pwr_nets[0],
                    "vout_net": pwr_nets[-1],
                    "sw_net": "",
                    "inductor_ref": "",
                    "diode_ref": "",
                })

    stage_by_vout: Dict[str, int] = {}
    for i, st in enumerate(stages):
        vout = str(st.get("vout_net", ""))
        if vout and vout not in stage_by_vout:
            stage_by_vout[vout] = i

    children: Dict[int, List[int]] = {i: [] for i in range(len(stages))}
    roots: List[int] = []
    for i, st in enumerate(stages):
        vin = str(st.get("vin_net", ""))
        parent = stage_by_vout.get(vin)
        if parent is None:
            roots.append(i)
        else:
            children[parent].append(i)

    ext_load_w: Dict[str, float] = {}
    for net in nets.keys():
        v = parse_rail_voltage(net)
        if v is None or v <= 0:
            continue
        refs_on_net = net_refs.get(net, set())
        conn_count = sum(1 for r in refs_on_net if ref_prefix(r) in {"CN", "J", "P", "X", "CON"} or r.startswith("CN"))
        if conn_count <= 0:
            i_guess = 0.08 if abs(v - 3.3) < 0.5 else (0.12 if abs(v - 5.0) < 0.8 else 0.05)
        else:
            if v <= 3.6:
                i_guess = 0.12 * conn_count
            elif v <= 5.5:
                i_guess = 0.20 * conn_count
            elif v <= 12.0:
                i_guess = 0.10 * conn_count
            else:
                i_guess = 0.05 * conn_count
        ext_load_w[net] = max(v * i_guess, 0.0)

    net_span = load_net_span_map(metrics)
    stage_result_cache: Dict[int, Dict[str, float]] = {}
    stage_breakdown: List[Dict[str, object]] = []
    ds_matched_refs: Set[str] = set()
    assumptions: List[str] = []

    def eval_stage(idx: int) -> float:
        if idx in stage_result_cache:
            return stage_result_cache[idx]["p_in_w"]

        st = stages[idx]
        ref = str(st.get("ref", ""))
        typ = str(st.get("type", ""))
        vin_net = str(st.get("vin_net", ""))
        vout_net = str(st.get("vout_net", ""))
        sw_net = str(st.get("sw_net", ""))
        inductor_ref = str(st.get("inductor_ref", ""))
        diode_ref = str(st.get("diode_ref", ""))

        vout = parse_rail_voltage(vout_net)
        if vout is None or vout <= 0:
            vout = 5.0 if typ == "acdc" else 3.3
            assumptions.append(f"{ref} output voltage unresolved, use default {vout:.2f}V")

        vin = parse_rail_voltage(vin_net)
        if typ == "acdc":
            vin = 311.0
        if vin is None or vin <= 0:
            vin = 5.0 if typ in {"buck", "linear"} else 311.0
            assumptions.append(f"{ref} input voltage unresolved, use default {vin:.2f}V")

        child_pin_w = sum(eval_stage(c) for c in children.get(idx, []))
        pout_w = float(ext_load_w.get(vout_net, 0.0)) + child_pin_w
        iout = pout_w / max(vout, 1e-6)

        comp = ref_catalog.get(ref.upper(), {"part": "", "lcsc": "", "desc": ""})
        part = str(comp.get("part", ""))
        lcsc = str(comp.get("lcsc", ""))
        desc = str(comp.get("desc", ""))

        comp_text, ds_files = find_component_text(ref, part, lcsc, desc)
        if ds_files:
            ds_matched_refs.add(ref)

        eff = extract_efficiency(comp_text)
        iq_a = extract_keyword_current(comp_text, ["quiescent current", "supply current", "no load current", "standby current", "operating current"])
        rated_iout = extract_keyword_current(comp_text, ["rated output current", "output current", "max output current"])
        rated_pout = extract_keyword_power(comp_text, ["output power", "rated power", "max power"])

        dcr = None
        if inductor_ref:
            lcomp = ref_catalog.get(inductor_ref.upper(), {"part": "", "lcsc": "", "desc": ""})
            ltext, _ = find_component_text(inductor_ref, str(lcomp.get("part", "")), str(lcomp.get("lcsc", "")), str(lcomp.get("desc", "")))
            dcr = extract_keyword_resistance(ltext, ["dcr", "dc resistance"])

        vf = None
        if diode_ref:
            dcomp = ref_catalog.get(diode_ref.upper(), {"part": "", "lcsc": "", "desc": ""})
            dtext, _ = find_component_text(diode_ref, str(dcomp.get("part", "")), str(dcomp.get("lcsc", "")), str(dcomp.get("desc", "")))
            vf = extract_keyword_voltage(dtext, ["forward voltage", "vf"])

        if typ == "buck":
            if eff is None:
                eff = 0.86
            if iq_a is None:
                iq_a = 0.005
            if dcr is None:
                dcr = 0.08
            if vf is None:
                vf = 0.55
            if iout < 0.01:
                iout = 0.01
                pout_w = iout * vout

            p_ind = iout * iout * dcr
            duty = min(max(vout / max(vin, 1e-6), 0.02), 0.98)
            p_diode = iout * (1.0 - duty) * vf
            p_iq = vin * iq_a
            p_loss = max(pout_w * (1.0 / max(eff, 1e-6) - 1.0), p_ind + p_diode + p_iq)
            span_sw = net_span.get(sw_net, 0.0)
            if span_sw > 1200.0:
                p_loss += 0.03
            elif span_sw > 700.0:
                p_loss += 0.015
            p_in = pout_w + p_loss
        elif typ == "linear":
            if iq_a is None:
                iq_a = 0.003
            p_loss = max((vin - vout) * iout, 0.0) + vin * iq_a
            p_in = pout_w + p_loss
            eff = pout_w / max(p_in, 1e-9)
        else:
            if eff is None:
                eff = 0.80
            no_load_p = extract_keyword_power(comp_text, ["no load", "standby power", "idle power"])
            if no_load_p is None:
                no_load_p = 0.25
            p_in = pout_w / max(eff, 1e-6) + no_load_p
            p_loss = p_in - pout_w

        if rated_iout and iout > 0.85 * rated_iout:
            assumptions.append(f"{ref} estimated Iout {iout:.3f}A approaches rating {rated_iout:.3f}A")
        if rated_pout and pout_w > 0.85 * rated_pout:
            assumptions.append(f"{ref} estimated Pout {pout_w:.3f}W approaches rating {rated_pout:.3f}W")

        stage_result_cache[idx] = {
            "p_in_w": p_in,
            "p_out_w": pout_w,
            "p_loss_w": p_loss,
            "eff": max(min((pout_w / p_in) if p_in > 1e-9 else 0.0, 1.0), 0.0),
        }

        stage_breakdown.append({
            "ref": ref,
            "type": typ,
            "vin_net": vin_net,
            "vout_net": vout_net,
            "i_out_a": round(iout, 6),
            "p_out_w": round(pout_w, 6),
            "p_loss_w": round(p_loss, 6),
            "eff_pct": round(100.0 * stage_result_cache[idx]["eff"], 2),
            "param_source": "datasheet" if ds_files else "default+netlist",
            "datasheet_files": ds_files,
        })
        return p_in

    total_input_w = 0.0
    for r in roots:
        total_input_w += eval_stage(r)

    known_from_resistors = 0.0
    resistor_info = metrics.get("resistor_electrical", {})
    if isinstance(resistor_info, dict):
        v = resistor_info.get("total_resistor_power_w")
        if isinstance(v, (int, float)):
            known_from_resistors = float(v)

    total_conversion_loss_w = sum(float(stage_result_cache[i]["p_loss_w"]) for i in stage_result_cache)
    total_output_w = sum(float(stage_result_cache[i]["p_out_w"]) for i in stage_result_cache if i in roots)

    total_w = total_input_w + known_from_resistors
    stages.sort(key=lambda x: str(x.get("ref", "")))
    stage_breakdown.sort(key=lambda x: str(x.get("ref", "")))

    metrics["power_model"] = {
        "known_only": False,
        "count_u": count_u,
        "count_q": count_q,
        "count_diode": count_diode,
        "stage_count": len(stages),
        "topology_summary": ", ".join(f"{s.get('ref')}:{s.get('type')}" for s in stages) if stages else "??????/??????????????",
        "total_output_load_w": round(total_output_w, 6),
        "total_conversion_loss_w": round(total_conversion_loss_w + known_from_resistors, 6),
        "known_resistor_power_w": round(known_from_resistors, 6),
        "method": "datasheet_topology_layout_fused",
        "datasheet_match": {
            "matched_components": len(ds_matched_refs),
            "scanned_pdf_count": len(pdf_text_cache),
        },
        "stage_breakdown": stage_breakdown,
        "assumptions": assumptions[:20],
    }
    metrics["estimated_power_w"] = round(total_w, 6)
    return total_w

def severity_rank(sev: str) -> int:
    order = {"high": 3, "medium": 2, "low": 1, "info": 0}
    return order.get(sev.lower(), 0)


def overall_level(findings: List[dict]) -> str:
    if any(f["severity"] == "high" for f in findings):
        return "HIGH"
    if any(f["severity"] == "medium" for f in findings):
        return "MEDIUM"
    return "LOW"


def counts_by_severity(findings: List[dict]) -> Dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return counts


def pcb_score_grade(score: int) -> str:
    if score >= 90:
        return "可直接打样"
    if score >= 80:
        return "适合打样验证"
    if score >= 70:
        return "整改后可打样"
    return "不建议直接下单"


def build_pcb_score(findings: List[dict], metrics: Dict[str, object]) -> Dict[str, object]:
    counts = counts_by_severity(findings)
    score = 100
    rows: List[List[str]] = [["基础分", "+100", "风险和规则修正前的基准分"]]

    def apply_delta(item: str, delta: int, note: str) -> None:
        nonlocal score
        score += delta
        rows.append([item, f"{delta:+d}", note])

    if counts["high"] > 0:
        apply_delta("高风险项", -22 * counts["high"], f"{counts['high']} 项，每项扣 22 分")
    else:
        rows.append(["高风险项", "+0", "无高风险项"])

    if counts["medium"] > 0:
        apply_delta("中风险项", -8 * counts["medium"], f"{counts['medium']} 项，每项扣 8 分")
    else:
        rows.append(["中风险项", "+0", "无中风险项"])

    if counts["low"] > 0:
        apply_delta("低风险项", -2 * counts["low"], f"{counts['low']} 项，每项扣 2 分")
    else:
        rows.append(["低风险项", "+0", "无低风险项"])

    spacing_mm = metrics.get("min_hv_lv_edge_mm")
    if isinstance(spacing_mm, (int, float)):
        spacing_mm_f = float(spacing_mm)
        if spacing_mm_f < 6.0:
            apply_delta("安全间距修正", -10, f"最小高低压间距 {spacing_mm_f:.3f} mm，小于 6.0 mm")
        elif spacing_mm_f < 8.0:
            apply_delta("安全间距修正", -3, f"最小高低压间距 {spacing_mm_f:.3f} mm，介于 6.0 到 8.0 mm")
        else:
            rows.append(["安全间距修正", "+0", f"最小高低压间距 {spacing_mm_f:.3f} mm，达到 8.0 mm 余量"])
    else:
        apply_delta("安全间距修正", -4, "缺少可计算的高低压间距数据")

    power_model = metrics.get("power_model", {}) if isinstance(metrics.get("power_model"), dict) else {}
    stage_count = int(power_model.get("stage_count", 0) or 0) if isinstance(power_model, dict) else 0
    if stage_count <= 0:
        apply_delta("功耗建模修正", -4, "未识别到可计算的电源拓扑级")
    else:
        rows.append(["功耗建模修正", "+0", f"已识别 {stage_count} 级电源拓扑"])

    score = max(0, min(100, int(round(score))))
    grade = pcb_score_grade(score)
    summary = f"总分 {score}/100，评级 {grade}"
    return {
        "score": score,
        "grade": grade,
        "rows": rows,
        "summary": summary,
        "counts": counts,
    }


def append_pcb_score(lines_task: List[str], score_data: Dict[str, object]) -> None:
    rows = score_data.get("rows", [])
    score = int(score_data.get("score", 0) or 0)
    grade = str(score_data.get("grade", ""))
    summary = str(score_data.get("summary", ""))

    lines_task.append("")
    lines_task.append("#PCB整体评分")
    lines_task.append(f"评分 {score}/100")
    lines_task.append(f"评级 {grade}")
    append_ascii_table(
        lines_task,
        ["评分项", "分值变化", "说明"],
        rows if isinstance(rows, list) else [],
        right_cols={1},
    )
    lines_task.append(f"评分结论 {summary}")


def sorted_findings(findings: List[dict]) -> List[dict]:
    return sorted(findings, key=lambda f: (-severity_rank(f["severity"]), f["title"]))


CN_EXACT_MAP = {
    "AGND and GND star-point not identified": "未识别到AGND与DGND单点连接",
    "AGND and DGND star-point not identified": "未识别到AGND与DGND单点连接",
    "AGND and DGND kept separated": "AGND与DGND保持分离",
    "Cost calculation incomplete": "成本计算不完整",
    "Cost estimate partially covered": "成本估算覆盖不完整",
    "ISO_GND reference bridge not identified": "未识别到ISO_GND参考桥接",
    "ISO_GND floating (no AC_N bridge)": "ISO_GND未桥接AC_N且处于浮地",
    "Gerber missing": "未找到Gerber文件",
    "Netlist missing": "未找到Netlist文件",
    "Voltage channel input boundary risk": "电压通道输入边界风险",
    "Current channel input boundary risk": "电流通道输入边界风险",
    "No flying probe geometry": "缺少飞针几何数据",
    "Datasheet auto-download partially incomplete": "数据手册自动下载未完全完成",
    "Datasheet sync reused cached results": "数据手册同步复用缓存结果",
    "Datasheet sync completed": "数据手册同步完成",
    "Component info source": "器件资料来源",
    "File management pending user decision": "文件管理等待用户确认",
    "File management skipped by user": "用户已跳过文件管理",
    "File management plan generated": "已生成文件管理计划",
    "Current channel input range acceptable": "电流通道输入范围正常",
    "Voltage channel input range acceptable": "电压通道输入范围正常",
    "Gerber key layers present": "Gerber关键层完整",
    "HV-LV spacing estimate": "高低压间距评估",
    "Auto BOM detected": "已自动识别BOM文件",
    "BOM auto-detection failed": "自动识别BOM失败",
    "Workspace file layout looks clean": "工作区文件结构较整洁",
    "DGND domain not identified": "未识别到DGND域",
    "AGND-DGND bridge identified": "已识别AGND与DGND桥接",
    "Multiple AGND-DGND bridges": "检测到多个AGND与DGND桥接",
    "Analysis profile selected": "已选择分析剖面",
    "Generic analysis mode": "通用分析模式",
    "Single analog ground domain": "单模拟地域",
    "Single digital ground domain": "单数字地域",
    "Potential floating nets detected": "检测到疑似悬空网络",
    "High fanout nets detected": "检测到高扇出网络",
    "Signal chain connectivity looks consistent": "信号链连通性整体正常",
    "Power decoupling missing on supply nets": "供电去耦可能不足",
    "Power decoupling density can improve": "供电去耦密度可优化",
    "Control nets may lack pull bias": "关键控制线可能缺少上拉下拉",
    "I2C pull-up may be missing": "I2C上拉可能缺失",
    "External interface protection may be weak": "外部接口防护可能偏弱",
    "Resistor electrical load acceptable": "电阻电气负载正常",
    "Resistor thermal margin warning": "电阻热裕量告警",
    "Resistor power risk": "电阻功耗风险",
    "Voltage current calculation incomplete": "电压电流计算不完整",
}

CN_REPLACE_MAP = {
    "No 0R or net-tie bridge detected between AGND and GND": "未检测到AGND与DGND之间的0欧或NetTie桥接",
    "No 0R or net-tie bridge detected between AGND and DGND": "未检测到AGND与DGND之间的0欧或NetTie桥接",
    "Add one explicit star-point if domains must be connected": "若需要共地 请增加一个明确单点连接",
    "BOM not provided": "未提供BOM文件",
    "Provide BOM CSV with quantity and unit price for exact cost": "请提供包含数量和单价的BOM用于精确成本计算",
    "Provide BOM csv or xlsx with quantity and unit price for exact cost": "请提供包含数量和单价的BOM csv或xlsx用于精确成本计算",
    "BOM XLSX has no rows": "BOM XLSX内容为空",
    "BOM XLS not supported directly please export CSV or XLSX": "BOM XLS暂不直接支持 请导出为CSV或XLSX",
    "Unsupported BOM format:": "不支持的BOM格式",
    "BOM parse failed:": "BOM解析失败",
    "No usable BOM rows detected": "BOM中未识别到可用于成本计算的器件行",
    "Partial BOM cost coverage": "BOM成本覆盖率",
    "No explicit AC_N-ISO_GND bridge detected": "未检测到显式AC_N到ISO_GND桥接",
    "Confirm intended isolated-front-end reference strategy": "请确认隔离前端参考策略是否符合设计意图",
    "No explicit AC_N-ISO_GND bridge detected. Floating isolated ground can be intentional": "未检测到显式AC_N到ISO_GND桥接 隔离地浮置在很多方案中是有意设计",
    "If signal reference is required add a controlled bridge and verify safety and EMC": "若需要信号参考 请增加受控桥接并验证安规与EMC",
    "Provide valid netlist": "请提供有效Netlist文件",
    "Provide valid Gerber zip": "请提供有效Gerber压缩包",
    "Increase margin by tuning divider and bias network": "通过调整分压与偏置网络增加余量",
    "Reduce burden or rebias input": "降低负载电阻或重新设置输入偏置",
    "Use --file-manage yes and optionally --file-manage-apply": "使用--file-manage yes 可选再加--file-manage-apply",
    "Maintain calibration and thermal checks": "保持标定并进行热稳定性检查",
    "Proceed to spacing and electrical checks": "继续进行间距与电气规则检查",
    "Keep current spacing and verify with final DRC": "保持当前间距并在最终DRC中复核",
    "Keep calibration for gain and offset": "保持增益和零点标定",
    "Estimated minimum HV-LV pad edge spacing:": "估算高低压最小焊盘边缘间距",
    "Copper layers detected:": "检测到铜层数量",
    "Provide BOM with LCSC code or datasheet URL for unresolved parts": "请提供带LCSC编码或datasheet链接的BOM以补全未匹配器件",
    "Downloaded ": "已下载 ",
    ", existed ": " 已存在 ",
    ", missing ": " 缺失 ",
    ". See ": " 详见 ",
    "Use BOM file ": "使用BOM文件 ",
    "Cost and datasheet logic will read this BOM automatically": "成本与数据手册流程将自动读取该BOM",
    "No BOM file found in module workspace ": "在模块目录未找到BOM文件 ",
    "Put BOM csv/xlsx/xls into module workspace or pass --bom": "请将BOM csv xlsx xls放到模块目录或使用--bom指定",
    "Use BOM components only (": "仅使用BOM器件（",
    " deduped) for datasheet updates": " 个去重后条目）进行数据手册更新",
    "Only changed BOM components will trigger refresh by cache signature": "仅在BOM器件发生变更时按签名刷新资料",
    "No BOM component change detected; reused cached datasheet state, unresolved count ": "未检测到BOM器件变更 已复用缓存数据手册状态 未解决数量 ",
    "Update BOM component model/LCSC code if you need to refresh unresolved datasheets": "如需刷新未解决器件 请更新BOM中的型号或LCSC编码",
    "No obvious clutter in module workspace. Skip file management prompt": "未检测到明显文件凌乱 跳过文件管理询问",
    "Continue current file structure": "保持当前文件结构即可",
    "Workspace appears cluttered.": "检测到工作区文件可能较乱",
    "Profile ": "剖面 ",
    "Profile generic (auto alias)": "剖面 generic 自动模式别名",
    "Profile generic": "剖面 generic",
    "Use --analysis-profile generic to override auto mode": "可用--analysis-profile generic覆盖自动识别",
    "Single-ground designs are acceptable. Add star-point only when cross-domain reference is required": "单地设计通常可接受 仅在需要跨域参考时增加单点连接",
    "Review intentional test stubs and tie unused inputs to defined levels": "检查测试焊盘是否有意保留 并将未使用输入固定到确定电平",
    "Check drive strength timing and buffering for heavy fanout nets": "检查高扇出网络的驱动能力 时序裕量和缓冲策略",
    "Proceed to timing and SI checks in PCB tool": "继续在PCB工具中进行时序与信号完整性检查",
    "No resistor branch has both-end nominal voltages resolvable from net names": "未找到可由网名解析双端标称电压的电阻支路",
    "Use standard rail names like 3V3 5V 12V AGND DGND to improve automatic calculations": "建议使用3V3 5V 12V AGND DGND等标准电源网名提升自动计算准确度",
    "Supply nets without decoupling capacitor to ground:": "未见到地去耦电容的供电网络：",
    "Supply nets with limited decoupling:": "去耦密度偏低的供电网络：",
    "Add local bypass capacitors close to IC supply pins and verify return path": "在芯片电源引脚附近增加旁路电容并核查回流路径",
    "Increase local decoupling density for heavily shared rails": "对多芯片共享电源轨提高本地去耦密度",
    "Control-like nets without obvious bias resistor:": "未见明显偏置电阻的控制网络：",
    "Add or verify pull-up/pull-down on reset enable boot control lines": "为复位使能启动等控制线补充或确认上下拉",
    "I2C-like nets without clear pull-up resistor:": "未见明确上拉电阻的I2C类网络：",
    "Add external pull-up resistors or document internal pull-up source": "增加外部上拉或在设计文档中注明内部上拉来源",
    "Connector-to-IC nets without obvious series/protection:": "连接器到芯片间未见串联或防护的网络：",
    "Consider series damping resistor and ESD or surge protection for external-facing nets": "对外接口考虑串联阻尼电阻及ESD浪涌防护",
    "Validate with measured rail voltages during bring-up": "在上电调试阶段用实测电源轨电压复核",
    "Ambiguous BOM price column detected use explicit unit price or line total header": "BOM价格列含义不明确 请使用单价或行总价等明确列名",
    "No obvious floating or extreme fanout nets detected": "未发现明显悬空网络或异常高扇出网络",
    "Neither DGND nor default GND net found": "未找到DGND或默认GND网络",
    "Define DGND net or use default GND consistently": "请定义DGND网络或统一使用默认GND",
    "Detected analog ground only:": "仅检测到模拟地",
    "Detected digital/default ground only:": "仅检测到数字或默认地",
    "This is acceptable for pure analog boards": "对于纯模拟板可接受",
    "This is acceptable for pure digital boards": "对于纯数字板可接受",
    "Increase creepage and clearance to >= 6 mm and verify routing-level clearance": "请将爬电与电气间隙提升到至少6mm并复核布线级间距",
    "Prefer >= 8 mm margin for robust mains-related design": "市电相关设计建议预留至少8mm安全余量",
    "Verify this matches isolation strategy and safety rules": "请确认该连接符合隔离策略和安规要求",
    "Keep bridge close to mixed-signal boundary": "建议将桥接点靠近混合信号边界",
}


def to_cn_text(text: str) -> str:
    m = re.match(r"Found (\d+) high fanout nets$", text)
    if m:
        return f"发现 {m.group(1)} 个高扇出网络"
    m = re.match(r"Found (\d+) single-pin nets possibly floating$", text)
    if m:
        return f"发现 {m.group(1)} 个可能悬空的单引脚网络"
    m = re.match(r"Computed (\d+) resistor branches total dissipation ([0-9.]+) W$", text)
    if m:
        return f"已计算 {m.group(1)} 个电阻支路 总耗散功率 {m.group(2)} W"
    m = re.match(r"Partial BOM cost coverage (\d+)/(\d+) rows$", text)
    if m:
        return f"BOM成本覆盖率 {m.group(1)}/{m.group(2)} 行"

    if text in CN_EXACT_MAP:
        return CN_EXACT_MAP[text]
    out = text
    for en, cn in CN_REPLACE_MAP.items():
        out = out.replace(en, cn)
    return out


def overall_level_cn(level: str) -> str:
    mapping = {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}
    return mapping.get(level, level)


def make_task_key(f: dict) -> str:
    base = f"{f['severity']}|{f['title']}|{f['action']}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def build_current_tasks(findings: List[dict]) -> Dict[str, dict]:
    tasks: Dict[str, dict] = {}
    for f in sorted_findings(findings):
        if f["severity"] not in ("high", "medium", "low"):
            continue
        key = make_task_key(f)
        tasks[key] = {
            "key": key,
            "severity": f["severity"],
            "title": to_cn_text(f["title"]),
            "detail": to_cn_text(f["detail"]),
            "action": to_cn_text(f["action"]),
        }
    return tasks


def load_previous_tasks(module_log_dir: Path) -> Dict[str, dict]:
    """Load latest task records from module-specific log folder."""
    log_dir = module_log_dir
    if not log_dir.exists():
        return {}
    logs = sorted(log_dir.glob("tasklog_*.log"))
    if not logs:
        return {}
    latest = logs[-1]
    previous: Dict[str, dict] = {}
    for line in read_text(latest).splitlines():
        if not line.startswith("task_record|"):
            continue
        parts = line.split("|")
        data: Dict[str, str] = {}
        for item in parts[1:]:
            if "=" in item:
                k, v = item.split("=", 1)
                data[k] = v
        key = data.get("key")
        if not key:
            continue
        previous[key] = {
            "key": key,
            "severity": data.get("severity", ""),
            "title": data.get("title", ""),
            "status": data.get("status", ""),
        }
    return previous


def group_tasks_by_severity(tasks: Dict[str, dict]) -> Dict[str, List[dict]]:
    grouped = {"high": [], "medium": [], "low": []}
    for t in tasks.values():
        grouped[t["severity"]].append(t)
    for sev in grouped:
        grouped[sev] = sorted(grouped[sev], key=lambda x: x["title"])
    return grouped


def append_cn_task_section(lines: List[str], title: str, items: List[dict]) -> None:
    lines.append(title)
    if not items:
        lines.append("无")
        return
    for i, item in enumerate(items, 1):
        lines.append(f"{i} {item['title']}")
        lines.append(f"※原因 {item['detail']}")
        lines.append(f"※改进 {item['action']}")


def write_outputs(
    log_dir: Optional[Path],
    module_dir: Path,
    module: str,
    stamp: str,
    args: argparse.Namespace,
    findings: List[dict],
    metrics: Dict[str, object],
) -> Tuple[Path, Optional[Path]]:
    log_mode_on = args.log_mode == "on"
    if log_mode_on and log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
    module_dir.mkdir(parents=True, exist_ok=True)

    tasklist = module_dir / "tasklist.txt"
    tasklog = (log_dir / f"tasklog_{module}_{stamp}.log") if (log_mode_on and log_dir is not None) else None

    counts = counts_by_severity(findings)
    overall = overall_level(findings)
    ordered = sorted_findings(findings)
    score_data = build_pcb_score(findings, metrics)
    metrics["pcb_score"] = score_data

    current_tasks = build_current_tasks(findings)
    previous_tasks = load_previous_tasks(log_dir) if (log_mode_on and log_dir is not None) else {}

    current_keys = set(current_tasks.keys())
    previous_keys = set(previous_tasks.keys())
    completed_keys = sorted(previous_keys - current_keys)
    unresolved_keys = sorted(previous_keys & current_keys)
    new_keys = sorted(current_keys - previous_keys)

    grouped = group_tasks_by_severity(current_tasks)

    lines_task: List[str] = []
    lines_task.append(f"#结论 {overall_level_cn(overall)}")
    lines_task.append(f"模块 {module}")
    lines_task.append(f"时间 {stamp}")
    if "gerber_analysis_dir" in metrics:
        lines_task.append(f"Gerber解析目录 {metrics['gerber_analysis_dir']}")
    lines_task.append(f"风险统计 高={counts['high']} 中={counts['medium']} 低={counts['low']} 信息={counts['info']}")
    lines_task.append("")
    append_cn_task_section(lines_task, "#高风险警告：", grouped["high"])
    lines_task.append("")
    append_cn_task_section(lines_task, "#中风险警告：", grouped["medium"])
    lines_task.append("")
    append_cn_task_section(lines_task, "#低风险提示：", grouped["low"])
    lines_task.append("")
    if log_mode_on:
        lines_task.append("#上次任务完成情况：")
        lines_task.append(f"1 已完成 {len(completed_keys)} 项")
        if completed_keys:
            for i, key in enumerate(completed_keys, 1):
                title = to_cn_text(previous_tasks.get(key, {}).get("title", key))
                lines_task.append(f"  {i}. {title}")
        lines_task.append(f"2 未完成 {len(unresolved_keys)} 项")
        if unresolved_keys:
            for i, key in enumerate(unresolved_keys, 1):
                title = to_cn_text(current_tasks.get(key, {}).get("title", key))
                lines_task.append(f"  {i}. {title}")
        lines_task.append(f"3 新增 {len(new_keys)} 项")
        if new_keys:
            for i, key in enumerate(new_keys, 1):
                title = to_cn_text(current_tasks.get(key, {}).get("title", key))
                lines_task.append(f"  {i}. {title}")
    else:
        lines_task.append("#历史追踪")
        lines_task.append("日志模式已关闭 不保留历史任务记录")

    append_pcb_score(lines_task, score_data)
    append_cost_table(lines_task, metrics, args.bom)
    append_power_analysis(lines_task, metrics)
    append_error_analysis(lines_task, metrics)
    append_netlist_layout_advice(lines_task, metrics)

    tasklist.write_text("\n".join(lines_task) + "\n", encoding="utf-8-sig")
    if log_mode_on and tasklog is not None:
        lines_log: List[str] = [
            f"timestamp={dt.datetime.now().isoformat(timespec='seconds')}",
            f"module={module}",
            f"gerber={args.gerber if args.gerber else ''}",
            f"netlist={args.netlist}",
            f"docs={','.join(str(p) for p in args.doc)}",
            f"pnp={args.pnp}",
            f"bom={args.bom}",
            f"overall={overall}",
            "metrics=" + json.dumps(metrics, ensure_ascii=True, sort_keys=True),
            f"task_progress|completed={len(completed_keys)}|unresolved={len(unresolved_keys)}|new={len(new_keys)}",
        ]
        for f in ordered:
            lines_log.append(
                f"finding severity={f['severity']} title={f['title']} detail={f['detail']} action={f['action']} source={f['source']}"
            )
        for key, task in sorted(current_tasks.items(), key=lambda x: x[0]):
            lines_log.append(
                f"task_record|key={key}|severity={task['severity']}|title={task['title']}|status=current_open"
            )
        for key in completed_keys:
            title = previous_tasks.get(key, {}).get("title", key)
            sev = previous_tasks.get(key, {}).get("severity", "")
            lines_log.append(f"task_record|key={key}|severity={sev}|title={title}|status=completed")
        tasklog.write_text("\n".join(lines_log) + "\n", encoding="utf-8")
    return tasklist, tasklog
