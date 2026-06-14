# state-tracker

A daemon that snoops the broker's ML / DL'80 / DL'86 Redis channels (both
`:receive` and `:transmit`) and keeps a decoded "what is the system doing
right now" view in three Redis keys, re-published on every change.

Unlike `ml-debug` / `dl-debug` (user-launched, print to the terminal), this
is a background service — `install.sh` enables `mdt-state.service`.

## Redis interface

| Key (poll with `GET`) | Channel (`SUBSCRIBE` for changes) | Bus |
|---|---|---|
| `state:ml`   | `link:ml:state`   | MasterLink |
| `state:dl80` | `link:dl80:state` | Datalink '80 (Beogram / Tape) |
| `state:dl86` | `link:dl86:state` | Datalink '86 (integrated music system) |

Each key holds a JSON blob; the matching channel publishes the same blob
whenever it changes (repeated identical status frames do **not** re-fire).

```sh
redis-cli GET state:ml
redis-cli GET state:dl86
redis-cli PSUBSCRIBE 'link:*:state'      # watch all three live
```

### `state:ml`
```json
{ "source": "0xa1", "source_name": "N.RADIO",
  "activity": "0x02", "activity_name": "Playing", "playing": true,
  "track": 5, "from": "0xc2", "origin": "rx",
  "updated": "2026-05-20T21:14:03.122" }
```
Tracked from `STATUS_INFO` (0x87) + `TRACK_INFO_LONG` (0x82). `playing`
is the simple "is it actually playing" flag (`activity == Playing`).

**Off / standby**: a `STANDBY` (0x10) or `RELEASE` (0x11) telegram, or a
virtual-Beo4 `STANDBY` keypress (0x0C), flips `activity` to Standby/Stop
and `playing` to `false`. `source_name` keeps the last source (so you can
see *what* was playing) — read `playing` / `activity_name` for the
on/off state.

**Robustness**: STATUS_INFO frames are accepted only when the source byte
is a known ML source. The bus carries short stub STATUS_INFO frames
(pl_len=0, where byte 10 is actually the checksum) and VM frames
advertising transient/non-audio bytes; both used to flicker the source to
junk like `0xe3 -> "?"`. Those are now dropped, so a good source isn't
clobbered. `origin` is `rx` (a real device announced it) or `tx` (our own
bridge did).

### `state:dl86`
```json
{ "to": "0x12", "to_name": "CD", "from_name": "Radio / BM",
  "transport": "playing", "track": 1, "volume": 20,
  "origin": "rx", "updated": "..." }
```
Tracked from 40-bit DL'86 STATUS frames: `transport`
(playing / stopped / lid-open / standby), `track` (CD track number),
`volume` (0..~78). `track` and `volume` are independent latched fields.

### `state:dl80`
```json
{ "device": "BG", "transport": "playing", "track": null,
  "last_opcode": "0xc3", "last_opcode_name": "BG.Status.Playing",
  "origin": "rx", "updated": "..." }
```
`device` is `BG` (Beogram) / `TP` (Tape), `transport` from the verified
single-byte status opcodes. **`track` is always `null`** — DL'80 track
number arrives via the multi-byte `F0`/`F4` TrackNum families whose 40-bit
argument layout is not yet verified, so we don't guess. Device + transport
are reliable.

## Running by hand

```sh
python3 /opt/mdt-tools/state-tracker/state_tracker.py
# or against a remote redis:
python3 state_tracker.py --redis-host other.host --redis-port 6379
```

## Dependencies

`redis-py` (apt `python3-redis`) and the broker (`mdtv2-broker.service`)
running so the `link:*` channels carry traffic. The decode tables here are
a self-contained copy of those in `ml-debug` / `dl-debug` (same convention
the repo uses — each tool carries its own copy).
