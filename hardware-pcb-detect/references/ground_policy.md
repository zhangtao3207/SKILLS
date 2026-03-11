# Ground Policy

## Ground Domains
- AGND: analog return domain
- GND or DGND: digital return domain
- ISO_GND: isolated primary-side local return
- PE: protective earth, chassis safety domain

## Recommended Strategy
- Keep AGND and DGND separated in routing and copper zones.
- Connect AGND and DGND at one controlled star point, often via 0R or net-tie.
- Keep ISO_GND isolated from AGND/DGND except intentional boundary elements.
- Do not connect PE and signal grounds arbitrarily.

## Fast Checks
- Verify each domain exists and is explicitly named.
- Verify intentional bridges are identifiable (for example 0R links).
- Flag accidental multiple bridges between same domains.
