# hardware-pcb-detect

| 项目 | 说明 |
|---|---|
| 技能名称 | hardware-pcb-detect |
| 主要用途 | 基于网表、Gerber 和模块文档检查原理合理性、PCB 可制造性和布局风险，并输出可执行改进建议 |
| 适用场景 | 接地与隔离策略检查、高低压间距评估、信号链完整性、功耗分析、误差分析、PCB 整体评分 |
| 必要输入 | 网表 `.tel`，模块说明 `.txt` |
| 可选输入 | Gerber `.zip`，BOM `.csv/.xlsx/.xls`，贴片文件 |
| 主要输出 | `tasklist.txt`、`datasheet/*.pdf`、`datasheet/_datasheet_sync_report.json`、`datasheet/_component_info_cache.json`、`*_gerber_analyse/*` |
| 输出特点 | 结论优先，中文任务风格，按严重级别分组，包含 PCB 整体评分、成本表、功耗分析、误差分析、网表驱动布局建议 |

| 常用命令 | 示例 |
|---|---|
| Gerber 变更检查 | `python scripts/gerber_change_guard.py --module MODULE --workspace-dir HARDWARE/MODULE` |
| 运行分析 | `python scripts/run_pcb_detect.py --module MODULE --workspace-dir HARDWARE/MODULE --netlist HARDWARE/MODULE/Netlist_Schematic.tel --doc HARDWARE/MODULE/Design_notes.txt --price-auto on --log-mode off` |
| 应用文件整理 | `python scripts/run_pcb_detect.py ... --file-manage yes --file-manage-apply` |

| 目录 | 作用 |
|---|---|
| `scripts/` | 分析与生成脚本 |
| `references/` | 规则与方法参考 |
| `agents/` | Skill 元数据 |
| `SKILL.md` | 技能主说明与约束 |
