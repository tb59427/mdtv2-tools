# Beogram 5500 — verified DL'80 commands

Tested 2026-05-02 against a Beogram 5500 connected to the mdtv2 bridge
via the DL'80 wire on the MDT HAT. Each command is a single byte
TX'd on the `link:dl80:transmit` Redis channel; the broker takes care
of the wire-level repeat that the DL'80 spec requires.

| Function | Hex | Mnemonic (DL'80 spec) | Redis command |
|---|---|---|---|
| Turn on / start playing | `a9` | `BG.Play` | `redis-cli PUBLISH link:dl80:transmit a9` |
| Skip to next track | `95` | `BG.ADV` | `redis-cli PUBLISH link:dl80:transmit 95` |
| Skip to previous track | `f3` | `BG.<-Ret` (PHRETURN) | `redis-cli PUBLISH link:dl80:transmit f3` |
| Turn off | `cb` | `Sys.Standby` | `redis-cli PUBLISH link:dl80:transmit cb` |

## Notes

- **Off uses `cb` (Sys.Standby), not `9a` (BG.Standby).** The protocol
  sheet documents `9a` as the dedicated "Beogram standby" code, but on
  the 5500 it has no observable effect (probably only honoured while
  the turntable is actively playing). `cb` reliably parks the arm and
  drops the platter. Note that `cb` is system-wide, so on a setup with
  other DL'80 devices on the same bus they will go to standby too.
- **Watch responses** while testing:
  ```sh
  redis-cli SUBSCRIBE link:dl80:receive
  ```
  Status echoes from the Beogram (e.g. `c3` Playing, `c5` Standby,
  `ce` Stopped) confirm the command was received and acted on.
- The DL'80 spec defines several other plausibly-relevant codes for
  Beograms (`cf` BG.Stop, `cd`/`c9` BG.Pause, `d5` BG.Start) but they
  haven't been tested on the 5500 yet — add a row above when you do.
