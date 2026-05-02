# dl-docs

Field-verified command notes for B&O Datalink-controllable devices. Each
subdirectory is one bus / family; each file inside is one specific device
that the command set was confirmed against (different units can vary
slightly).

| Subdir | Bus | Devices |
|---|---|---|
| `dl80-beogram/` | DL'80 (3.125 ms unit, single-byte commands) | Beogram turntables |

The full DL'80 protocol map (every documented opcode, including ones we
have not personally tested) lives in `Datalink 80 Protocol.xlsx` outside
this repo. The notes here are the subset that **we have actually sent
and observed working** against a real device.
