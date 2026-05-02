# dl-debug

Pretty-prints DL'80 and DL'86 traffic from the redis broker. Mirrors
`ml-debug/` but for the two Datalink protocols. User-launched, no
service.

## Use

```sh
sudo python3 /opt/mdt-tools/dl-debug/dl_debug.py
# or, more typically (sudo only needed if redis is locked down):
python3 /opt/mdt-tools/dl-debug/dl_debug.py
```

Subscribes to `link:dl80:transmit`, `link:dl80:receive`,
`link:dl86:transmit`, and `link:dl86:receive`. One decoded line per
message. Stop with Ctrl-C.

## Sample output

```
[dl-debug] subscribing to: link:dl80:receive, link:dl80:transmit, link:dl86:receive, link:dl86:transmit

[20:48:09.123] dl80 TX  0xA9  Sys.Src=>BG.Play          PHPLY      ×2
[20:48:09.580] dl80 RX  0xC3  BG.Status.Playing         SPHON      ×2
[20:48:14.456] dl86 TX  17b 00C920    Beo4=CD (0x92)  empirical-form
[20:48:14.700] dl86 RX  40b 3BD248540A  STATUS  from=Radio / BM  to=CD  payload=0x48540A
[20:48:14.901] dl86 RX  40b 3BD2400000  STATUS  from=Radio / BM  to=CD  payload=0x400000
[20:48:30.011] dl80 TX  0xCB  Sys.Standby               OFFSYSTM   ×2
```

* DL'80 dedup is on by default (each byte goes on the wire twice; we
  coalesce and show `×2`). Pass `--no-dedup-dl80` to see both copies.
* DL'86 is always shown one frame per line — the firmware decodes the
  full bit stream so there's no doubling to coalesce.

## Flags

```
--redis-host HOST          (default: localhost)
--redis-port PORT          (default: 6379)
--rx-only                  only messages broker -> host
--tx-only                  only messages host -> broker
--dl80-only                only DL'80 traffic
--dl86-only                only DL'86 traffic
-v, --verbose              expand DL'86 bit fields per line
--no-color                 disable ANSI color
--no-dedup-dl80            show both copies of each DL'80 byte
```

`-v` for DL'86 prints the bit breakdown:

```
[20:48:14.456] dl86 TX  17b 00C920    Beo4=CD (0x92)  empirical-form
    prefix     000000001
    Beo4 key   10010010 = 0x92  CD
```

## What it decodes

### DL'80

Every byte mapped to its mnemonic + descriptive name from the
"Datalink 80 Protocol" sheet. ~88 known opcodes:

* Source select / play: `A9` BG.Play, `AB` TP.Play, …
* Transport: `95` BG.ADV (next), `F3` BG.<-Ret (prev), `CF` BG.Stop,
  `CD` BG.Pause, `D5` BG.Start, `B5` TP.Stop, `AF` TP.<<-RW, `B1` TP.FF, …
* Standby: `9A` BG.Standby, `9B` TP.Standby, `CB` Sys.Standby
* Status replies: `C1` TP.Status.Standby, `C3` BG.Status.Playing,
  `C5` BG.Status.Standby, `B7` TP.Status.Playing, …
* Multi-byte status (5-arg families): `F0`, `F2`, `F4`, `F6`, `F8`,
  `FA` — the debugger flags these as multi-byte; the following
  bytes appear as separate lines (the broker emits each byte
  individually). Verbose decoding of the full multi-byte payload
  is not implemented yet.
* `FC` Sys.Release, `FD` BG.Status.Load, `EF` Sys.ShowStatus, …
* Number keys 0-9 (`81`, `83`, `85`, `87`, `89`, `8B`, `8D`, `8F`,
  `91`, `93`)

Anything not in the table prints as `unknown(0xXX)`.

### DL'86

Format detected by bit count:

| Bit count | Format | Decoded as |
|---|---|---|
| 6 | sys-level | `100101` recognised as `Sys.Standby` |
| 8 | auxcd short | `0x20` recognised as `AuxCD.Play` |
| 12 | auxcd + 4-bit prefix | same auxcd commands |
| **17 (a)** | **empirical** `00000000.1.<Beo4 key>` | the form verified on at least one music system; pulls the Beo4 key from the low byte |
| **17 (b)** | **BeoBabble AC** `10.00.<5-addr>.<8-cmd>` | classic form from BeoBabble docs |
| 20 | AUC | `10.00.<5-addr>.<3-unit>.<8-cmd>` |
| 22 | AAC | `10.10.<5-To>.<5-From>.<8-cmd>` |
| 40 | STATUS | `00.1110.<5-from>.<5-to>.<4-sub>.<4-mod>.<8-d1>.<8-d2>` |

Beo4 keys decoded by name (Standby, Step Up, Step Down, CD, Radio,
A.MEM, etc.). Address codes mapped to names where known (MCL, Radio,
CD, BM).

Unknown bit counts print as `<n>b (no decoder)` — the bits are still
shown as hex so you can inspect them by hand.

#### STATUS frame payload decoding

The 24-bit payload of a 40-bit STATUS frame is split into:

| Field | Width | Meaning |
|---|---|---|
| subtype | 4 bits | `0100` Status, `0110` RecStatus, `0000` Idle?, others marked `?subN` |
| modifier | 4 bits | source-specific flags (raw hex shown) |
| data1 | 8 bits | source-specific. A few common codes are named (`0x00` idle/no media, `0x40` stopped/standby, `0x48` play start) |
| data2 | 8 bits | further source-specific data (track number, counter byte, etc. — raw hex shown) |

Add new entries to `DL86_STATUS_SUBTYPES` and `DL86_STATUS_STATE` in
`dl_debug.py` as you observe + verify more codes — the dicts are
intentionally short, listing only what's been seen on real bus traffic.

Sample decoded output for the kind of frames you see from a CD module:

```
[20:48:14.700] dl86 RX  40b 3BD248540A  STATUS  from=Radio / BM  to=CD  Status  d1=0x54  d2=0x0A
[20:48:14.901] dl86 RX  40b 3BD2400000  STATUS  from=Radio / BM  to=CD  Status  d1=0x00 (idle / no media)  d2=0x00
```

Verbose (`-v`) prints the full bit breakdown:

```
[20:48:14.700] dl86 RX  40b 3BD248540A  STATUS  from=Radio / BM  to=CD  Status  d1=0x54  d2=0x0A
    type       STATUS (00.1110.<from>.<to>.<sub>.<mod>.<d1>.<d2>)
    from       11110 = 0x1E  Radio / BM
    to         10010 = 0x12  CD
    subtype    0100 = 0x4  Status
    modifier   1000 = 0x8
    data1      01010100 = 0x54
    data2      00001010 = 0x0A
```

## What it depends on

* `redis-py` (`apt install python3-redis`)
* The broker (`mdtv2-broker.service`) running, with the ATtiny826 HAT
  connected to whichever Datalink line you're snooping.
