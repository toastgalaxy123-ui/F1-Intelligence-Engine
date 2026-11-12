# Stage 4.1 — Official FIA circuit-reference audit

FIA access is active. 34 race weekends were checked; **17** have pre-forecast event-specific speed evidence and **32** have dated circuit/pit-layout documents. Text evidence uses an explicit event-speed directive or stewards' statement. Visual speed signs are separately identified. Unknown speeds remain missing. No route length was estimated from an unscaled drawing.

This audit was added after the model selection and primary test results were frozen. The selected model remains **P1**. No model weights, calibration, eligibility or primary results were changed. New reference CSVs live in a separate subdirectory and do not enter the frozen study's input loader. They are available for a future preregistered feature study; this audit establishes no forecast improvement.

## Availability and provenance

Each source has its official URL, SHA-256, document date, archive publication date and exact text quote or identified visual observation. Availability is conservatively the later of the printed date and archive publication date, at end-of-day UTC. Each accepted source precedes that race's first saved forecast. Drawings have source-document identities, which are not claimed to be physical geometry version numbers. The catalog records stated circuit changes separately. Provider report timestamps still lack independently matched legal entry/exit timing boundaries.

## Fixed-model test characterization

| Session | Circuit | Fixed model | Sourced km/h | Layout source | Lane n | Lane mean s | Lane MAE s | Net n |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 9920 | Zandvoort | P1 | — | True | 39 | 18.975 | 3.563 | 2 |
| 9858 | Las Vegas | P1 | 80.0 | True | 20 | 23.029 | 1.921 | 8 |
| 9963 | Montreal | P1 | 80.0 | True | 13 | 18.208 | 5.641 | 0 |
| 9947 | Silverstone | P1 | — | True | 35 | 29.637 | 5.788 | 0 |
| 9904 | Baku | P1 | 80.0 | True | 19 | 20.951 | 3.099 | 11 |
| 9850 | Lusail | P8 reviewed net cohort | 80.0 | True | 8 | 29.255 | 5.047 | 8 |

Known pit speed alone does not supply distance, deceleration, acceleration, service or on-track counterfactual movement. Lusail's dated drawings depict an 80 sign; Montreal's pre-race stewards' decision explicitly sets 80 km/h, and Baku's dated drawing also depicts 80. These sources do not establish a slower Lusail speed restriction as the explanation for its roughly +4 s net bias. Route geometry and reference bias remain unresolved. Lane and net populations differ; the Lusail row uses the eight previously reviewed P8 net cases. This is descriptive evidence about fixed predictions, not another model selection exercise. The lack of usable Montreal/Silverstone net targets and independent VSC/SC references is unchanged.

## Source-verified speed references

| Season | Session | Circuit | km/h | Evidence | Available UTC |
| --- | --- | --- | --- | --- | --- |
| 2023 | 7953 | Sakhir | 80 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2023%20Bahrain%20Grand%20Prix%20-%20Offence%20-%20Car%2010%20-%20Pit%20lane%20speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2023-03-04T23:59:59.999999+00:00 |
| 2023 | 9118 | Spielberg | 80 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2023%20Austrian%20Grand%20Prix%20-%20Infringement%20-%20Car%2044%20-%20Pit%20Lane%20Speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2023-06-30T23:59:59.999999+00:00 |
| 2023 | 9157 | Monza | 80 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2023%20Italian%20Grand%20Prix%20-%20Infringement%20-%20Car%201%20-%20Pit%20Lane%20Speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2023-09-01T23:59:59.999999+00:00 |
| 2023 | 9165 | Singapore | 60 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2023%20Singapore%20Grand%20Prix%20-%20Race%20Director's%20Event%20Notes.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2023-09-14T23:59:59.999999+00:00 |
| 2024 | 9496 | Suzuka | 80 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2024%20Japanese%20Grand%20Prix%20-%20Infringement%20-%20Car%2018%20-%20Pit%20Lane%20Speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2024-04-05T23:59:59.999999+00:00 |
| 2024 | 9515 | Imola | 80 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2024%20Emilia%20Romagna%20Grand%20Prix%20-%20Infringement%20-%20Car%2011%20-%20Pit%20Lane%20Speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2024-05-17T23:59:59.999999+00:00 |
| 2024 | 9582 | Zandvoort | 60 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2024%20Dutch%20Grand%20Prix%20-%20Race%20Director's%20Event%20Notes.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2024-08-22T23:59:59.999999+00:00 |
| 2024 | 9606 | Singapore | 60 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2024%20Singapore%20Grand%20Prix%20-%20Race%20Director's%20Event%20Notes.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2024-09-19T23:59:59.999999+00:00 |
| 2024 | 9644 | Las Vegas | 80 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2024%20Las%20Vegas%20Grand%20Prix%20-%20Infringement%20-%20Car%2014%20-%20Pit%20lane%20speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2024-11-23T23:59:59.999999+00:00 |
| 2024 | 9655 | Lusail | 80 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2024%20Qatar%20Grand%20Prix%20-%20Event%20Notes%20-%20Circuit%20Map,%20Pit%20Lane%20and%20Red%20Zone.pdf) — VERIFIED_OFFICIAL_MAP_SPEED_SIGN | 2024-11-28T23:59:59.999999+00:00 |
| 2024 | 9662 | Yas Marina Circuit | 80 | [FIA document](https://www.fia.com/sites/default/files/decision-document/2024%20Abu%20Dhabi%20Grand%20Prix%20-%20Infringement%20-%20Car%2037%20-%20Pit%20Lane%20Speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2024-12-06T23:59:59.999999+00:00 |
| 2025 | 10006 | Suzuka | 80 | [FIA document](https://www.fia.com/system/files/decision-document/2025_japanese_grand_prix_-_infringement_-_car_12_-_pit_lane_speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2025-04-04T23:59:59.999999+00:00 |
| 2025 | 9987 | Imola | 80 | [FIA document](https://www.fia.com/system/files/decision-document/2025_emilia_romagna_grand_prix_-_infringement_-_car_55_-_pit_lane_speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2025-05-16T23:59:59.999999+00:00 |
| 2025 | 9858 | Las Vegas | 80 | [FIA document](https://www.fia.com/system/files/decision-document/2025_las_vegas_grand_prix_-_infringement_-_car_44_-_pit_lane_speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2025-11-22T23:59:59.999999+00:00 |
| 2025 | 9963 | Montreal | 80 | [FIA document](https://www.fia.com/system/files/decision-document/2025_canadian_grand_prix_-_infringement_-_car_81_-_pit_lane_speeding.pdf) — VERIFIED_OFFICIAL_SPEED_ONLY | 2025-06-13T23:59:59.999999+00:00 |
| 2025 | 9904 | Baku | 80 | [FIA document](https://www.fia.com/system/files/decision-document/2025_azerbaijan_grand_prix_-_event_notes_-_circuit_map_pit_lane_quarantine_zone_and_red_zone.pdf) — VERIFIED_OFFICIAL_MAP_SPEED_SIGN | 2025-09-18T23:59:59.999999+00:00 |
| 2025 | 9850 | Lusail | 80 | [FIA document](https://www.fia.com/system/files/decision-document/2025_qatar_grand_prix_-_event_notes_-_circuit_map._pit_lane_drawing_emergency_exits_map_ers_battery_containment_area_red_zones_map.pdf) — VERIFIED_OFFICIAL_MAP_SPEED_SIGN | 2025-11-27T23:59:59.999999+00:00 |

The complete source catalog is `data/raw/reference/stage4_1_fia/catalog.json`; load the separate `pit_lane_reference.csv` explicitly for subsequent studies. Raw PDFs and extracted text are retained in the ignored run's `reference_sources` directory. Reproduce collection and audit with `python -m f1ri.validation.pit_fia_reference_audit --fetch`. Stage 5 remains untouched.
