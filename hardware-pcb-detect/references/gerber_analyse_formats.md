# Gerber Analyse Formats

## Goal
Generate a small, stable, agent-friendly file set when reading a Gerber zip.

## Required Folder Naming
`<gerber_zip_dir>/<module_lower>_gerber_analyse`

`module_lower` is normalized from `--module` using lowercase English letters and digits.

## Stable File Set
- `00_manifest.json`
- `01_layers_summary.csv`
- `02_components.csv`
- `03_pins.csv`
- `04_nets_summary.csv`
- `05_readme.txt`

## Why These Formats
- JSON: best for structured metadata and deterministic key lookup.
- CSV: fast for tabular scans and filtering in scripts and agents.
- TXT: concise human-readable guide without extra parsing overhead.

## Parsing Priority for Agents
1. `00_manifest.json`
2. `04_nets_summary.csv`
3. `03_pins.csv`
4. `01_layers_summary.csv`
5. `02_components.csv`
6. `05_readme.txt`
