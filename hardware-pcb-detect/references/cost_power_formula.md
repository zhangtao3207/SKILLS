# Cost and Power Formula

## Cost
If BOM has quantity and unit price columns:
- total_cost = sum(quantity_i * unit_price_i)

If BOM has line total column only:
- total_cost = sum(line_total_i)

If no valid BOM price fields:
- Return `insufficient input`.

## Power
Use a staged model, not a single rough sum.

1. Build topology from netlist:
- Detect `AC/DC`, `buck`, and `linear` stages from `U/L/D` and net relationships.
- Build parent-child rail dependency (`child Vin` fed by `parent Vout`).

2. Parse datasheet parameters per stage (when available):
- Efficiency (`efficiency %`)
- Quiescent/supply current
- Diode forward voltage (`Vf`)
- Inductor `DCR`
- Rated output current/power (for margin warning)

3. Estimate load and back-calculate input:
- Assign external rail load assumptions from net voltage and connector presence.
- For each stage:
  - `Pout = Vout * Iout`
  - Buck loss includes efficiency loss + `I^2*DCR` + diode conduction + quiescent current
  - Linear loss includes `(Vin - Vout) * Iout + Vin*Iq`
  - AC/DC stage uses efficiency + no-load power
- Sum root-stage `Pin` as total input power.

4. Add layout-aware penalty:
- Use Gerber `04_nets_summary.csv` net span as a proxy of high-current loop quality.
- Apply small penalty to switching stage loss when SW loop span is large.

5. Output requirements:
- `estimated_power_w`
- stage breakdown: `Ref`, topology, `Vin->Vout`, `Iout`, `Pout`, `Ploss`, `Efficiency`, source (`datasheet` or fallback)
- datasheet coverage count and assumptions list
- mark as engineering estimate that still requires bench validation.

## Required Data for Better Accuracy
- Rail voltages and currents per domain
- Duty cycle and operating modes
- Real load profile
- Full BOM with valid MPN/LCSC and datasheet links
- Measured rail currents and thermal test data for calibration
