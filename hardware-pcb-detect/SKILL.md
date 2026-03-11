---
name: hardware-pcb-detect
description: Detect PCB schematic-principle reasonableness and Gerber manufacturability consistency from netlist .tel, Gerber zip, and module docs. Use when checking grounding and isolation strategy, high-low voltage spacing, signal-chain integrity, impedance and crosstalk risks, and actionable PCB fixes.
---

# Hardware PCB Detect

## Overview
Use this skill to evaluate one hardware module with three priorities:
1. Analyze schematic feasibility from netlist and detect design errors or weak points.
   Focus on supply decoupling, control-net pull bias, interface protection, grounding and signal-chain consistency.
2. Analyze Gerber and evaluate PCB manufacturability and physical risk.
3. Run holistic intelligent assessment and output practical improvements.

Output concise Chinese conclusions first, then evidence and fixes.

## Workflow
1. Collect module inputs with priority on netlist tel Gerber and module docs.
2. Run `scripts/gerber_change_guard.py`.
3. If workspace contains `pcb_data` folder, use it as primary source directory.
4. If `--gerber` omitted, auto-pick latest Gerber zip in workspace or `pcb_data`.
5. If Gerber is missing, notify user immediately and stop heavy analysis.
6. Detect workspace clutter first. Ask user about file management only when clutter is detected.
7. Run `scripts/run_pcb_detect.py`.
8. Auto-sync datasheets to module `datasheet` folder when possible.
9. Do not download datasheets for simple parts like resistor capacitor inductor switch button; record their specs in `datasheet/others.txt`.
10. Use BOM-component signature cache. Refresh component datasheet/price only when BOM component content changes.
11. Auto-detect BOM in module workspace and parse cost from csv or xlsx. If unit/line price is missing, auto-query by BOM model/LCSC code and record estimated cost. If only xls is found, report conversion suggestion.
12. Append a cost table at the end of `tasklist.txt`.
13. Use generic analysis as the only profile and keep checks module-agnostic.
14. Apply schematic reasonability heuristics for optimization opportunities (decoupling density, pull-up/pull-down completeness, connector protection).
15. Append a Chinese `#PCB整体评分` section in `tasklist.txt` with traceable score breakdown and recommendation grade.
16. Append Chinese power analysis and error analysis sections in `tasklist.txt`.
17. For power analysis, parse component datasheets (when available), identify power topology from netlist, and apply layout-aware penalties from Gerber net span data.
18. In `tasklist.txt`, include Chinese stage-level power breakdown (Vin->Vout, Iout, Pout, Ploss, efficiency, parameter source).
19. In `tasklist.txt`, append a netlist-driven section `#基于网表的PCB布局建议与注意事项` with concrete layout guidance and cautions.
20. Read generated analysis bundle and tasklist.
21. Return conclusion first in Chinese.

## Input Contract
Required:
- Netlist `.tel`
- Module `.txt` docs

Gerber:
- Optional explicit `--gerber <zip>`
- Or auto-detect latest Gerber zip in module workspace

Optional:
- Pick and place file
- BOM csv xlsx xls
- Existing datasheet files
- `--analysis-profile auto|generic`

Unsupported or limited:
- Images only without engineering files
- Exact impedance/crosstalk numbers without stackup geometry

## Output Contract
Must output files:
- Module root folder: `tasklist.txt` (not inside `pcb_data`)
- `tasklist.txt` must include a cost table at the end
- `tasklist.txt` must include `#PCB整体评分` with a 0-100 score, breakdown rows, and recommendation grade
- `tasklist.txt` must include `#功耗分析` and `#误差分析` sections in Chinese
- `tasklist.txt` must include `#基于网表的PCB布局建议与注意事项` with actionable suggestions
- Datasheet folder: `datasheet/*.pdf` and `_datasheet_sync_report.json`
- Component info cache: `datasheet/_component_info_cache.json`
- Simple parts record: `datasheet/others.txt`
- Optional file management plan: `file_management_plan.txt`

Must auto-generate Gerber analysis bundle:
- Folder: `<gerber_zip_dir>/<module_lower>_gerber_analyse`
- Files:
- `00_manifest.json`
- `01_layers_summary.csv`
- `02_components.csv`
- `03_pins.csv`
- `04_nets_summary.csv`
- `05_readme.txt`

Do not output:
- Images
- Garbled text

## Commands
Gerber change trigger check:
```bash
python scripts/gerber_change_guard.py \
  --module MODULE \
  --workspace-dir HARDWARE/MODULE
```

Run analysis:
```bash
python scripts/run_pcb_detect.py \
  --module MODULE \
  --workspace-dir HARDWARE/MODULE \
  --netlist HARDWARE/MODULE/Netlist_Schematic.tel \
  --doc HARDWARE/MODULE/Design_notes.txt \
  --pnp HARDWARE/MODULE/PickAndPlace.csv \
  --price-auto on \
  --log-mode off
```

Apply file management:
```bash
python scripts/run_pcb_detect.py ... --file-manage yes --file-manage-apply
```

## Rules
- Conclusion first.
- Keep summary short.
- Group details by severity.
- Use Chinese task style in `tasklist.txt`.
- Keep PCB score traceable: show deduction items and avoid opaque black-box scoring.
- Keep power section headings and table items in Chinese.
- Log mode defaults to off to avoid disk growth.
- Keep the skill module-agnostic; do not hardcode project-specific or ADC-only assumptions.
- Power numbers must be traceable: state assumptions, datasheet coverage, and per-stage loss composition.

## References
Load only what you need:
- `references/input_constraints.md`
- `references/clearance_rules.md`
- `references/ground_policy.md`
- `references/cost_power_formula.md`
- `references/error_model.md`
- `references/gerber_analyse_formats.md`

## Sync Requirement
Keep both copies in sync:
- Global: `C:/Users/zhangtao/.codex/skills/hardware-pcb-detect`
- Project: `C:/Users/zhangtao/Desktop/PQM/.codex/skills/hardware-pcb-detect`

Log mode defaults to off. Enable logs only when required using `--log-mode on`.
