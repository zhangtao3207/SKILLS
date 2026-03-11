# Error Model

## Typical Hardware Error Sources
- Divider ratio tolerance and drift
- Reference drift and buffer load error
- Converter linearity and offset (for sampling channels)
- Amplifier offset and gain error
- CT/PT ratio and phase error
- Layout parasitic and EMI coupling

## Recommended Error Report
- Separate gain-like error and offset-like error
- Report worst-case and expected-case estimates
- Mark assumptions clearly

## Inputs Needed for Quantitative Error Budget
- Component tolerance and tempco
- Reference and amplifier datasheet limits
- Converter TUE/ENOB (or equivalent dynamic accuracy metric) in operating bandwidth
- Calibration strategy in digital/software processing chain
