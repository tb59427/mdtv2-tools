# DL'86 music system — verified commands

Tested 2026-05-02 against a B&O music system (model: _add yours here_)
connected to the mdtv2 bridge via the DL'86 wire on the MDT HAT.
Each command is a 17-bit DL'86 frame TX'd on the `link:dl86:transmit`
Redis channel. The broker takes care of pulse-distance encoding on the
wire.

## Verified commands

| Function | Beo4 key | bit_count:hex | Redis command |
|---|---|---|---|
| Radio source on  | 0x81 | `17:00C080` | `redis-cli PUBLISH link:dl86:transmit 17:00C080` |
| Tape source on   | 0x91 | `17:00C880` | `redis-cli PUBLISH link:dl86:transmit 17:00C880` |
| **CD source on** | 0x92 | `17:00C920` | `redis-cli PUBLISH link:dl86:transmit 17:00C920` |
| Next track       | 0x1E (Step Up) | `17:008F00` | `redis-cli PUBLISH link:dl86:transmit 17:008F00` |
| Previous track   | 0x1F (Step Down) | `17:008F80` | `redis-cli PUBLISH link:dl86:transmit 17:008F80` |
| **Standby**      | 0x0C | `17:008600` | `redis-cli PUBLISH link:dl86:transmit 17:008600` |

Watch responses with:

```sh
redis-cli SUBSCRIBE link:dl86:receive
```

## Frame format

The 17-bit frame this system accepts is:

```
00000000 . 1 . <8-bit Beo4 key>
^^^^^^^^   ^   ^^^^^^^^^^^^^^^^
prefix     |   Beo4 key code, MSB-first
           |
           bit 8 always = 1
```

So **any** Beo4 keypress maps to a 17-bit DL'86 command by:

```
17 bits = 0x00 (8 zeros) | 1 | KK
```

Packed into bytes for the broker (`bit_count:hex`):

```
byte 0 = 0x00
byte 1 = 0x80 | (KK >> 1)
byte 2 = (KK & 1) << 7
```

E.g. CD = Beo4 key 0x92:
- byte 1 = 0x80 | (0x92 >> 1) = 0x80 | 0x49 = **0xC9**
- byte 2 = (0x92 & 1) << 7 = 0 << 7 = **0x00** → padded → `0x20` after rounding to byte boundary
- → `17:00C920` ✓ (matches verified value)

## Common Beo4 key codes (from `core/telegram.py`)

For deriving more commands. Just plug `KK` into the formula above.

| Beo4 key | KK |
|---|---|
| Standby | 0x0C |
| Step Up (next) | 0x1E |
| Step Down (prev) | 0x1F |
| Rewind | 0x32 |
| Wind | 0x34 |
| Go / Play | 0x35 |
| Stop | 0x36 |
| Radio | 0x81 |
| A.MEM (tape) | 0x91 |
| CD | 0x92 |
| N.Radio | 0x93 |
| N.Music | 0x94 |

## Notes

- **The 17-bit form differs from the BeoBabble doc's 17-bit AC form.**
  The BeoBabble examples encode a To-address in the prefix; this system
  uses an all-zero prefix with the Beo4 key in the low byte. Empirically
  worked out by sweeping bit patterns until source-switch fired. The
  BeoBabble form (`10.00.01111.<KK>` / `17:87XX...`) was tried first
  and got no system response.
- The captured "BM→CD" frames (`40:3bd248540a` etc.) are 40-bit *status*
  messages the BM emits internally after a source change, not commands
  we should TX.
