# Input Constraints

## Required
- Gerber zip of the target module
- Netlist `.tel`
- Module `.txt` design documents

## Optional
- Pick and place file (`.csv`)
- Datasheet PDFs
- BOM CSV for cost calculations

## Unsupported or Limited
- Screenshot-only input
- Exact impedance and crosstalk numbers without stackup, dielectric constant, copper thickness, line width/spacing, and target impedance
- Accurate cost output without BOM price and quantity columns

## Fallback Policy
- Continue analysis with available files.
- Mark missing-data sections as `insufficient input`.
- Provide next-step data requirements.
