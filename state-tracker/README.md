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

ML has two independent masters, so the blob is split into an **`am`**
(Audio Master / audio path) and a **`vm`** (Video Master / video path)
view, plus a top-level `updated`:

```json
{
  "am": { "source": "0x8d", "source_name": "CD",
          "activity": "0x02", "activity_name": "Playing", "playing": true,
          "track": 7, "from": "0xc1", "origin": "rx" },
  "vm": { "source": "0x1f", "source_name": "DTV",
          "activity": "0x02", "activity_name": "Playing", "playing": true,
          "track": null, "from": "0xc0", "origin": "rx" },
  "updated": "2026-06-14T16:17:55.213"
}
```

Each source-carrying telegram is routed to a slot by **source category**
(audio sources → `am`, video sources → `vm`), *not* by who sent it — so
an audio source announced by the Source Center (`0xc2`, e.g. our own
AirPlay N.RADIO) still lands in `am` without giving the non-master SC its
own slot, and a video `STATUS_INFO` can never overwrite the active audio
source (or vice-versa). `playing` is the simple `activity == Playing`
flag. `from` records which device last updated that slot.

Tracked from `STATUS_INFO` (0x87), `TRACK_INFO_LONG` (0x82),
`TRACK_INFO` CURRENT_SOURCE (0x44 kind 0x05), and
`REQUEST_DISTRIBUTED_SOURCE` replies (0x08, source at the reply's
`raw[13]` — rides the link-join handshake and the startup query below).

**Off / standby**: a `STANDBY` (0x10) / `RELEASE` (0x11) telegram, or a
virtual-Beo4 `STANDBY` keypress (0x0C), flips the affected slot(s) to
Standby/Stop and `playing` to `false` while keeping `source_name` (so you
can still see what *was* playing). A source-less standby idles whatever
was playing in both slots.

**`vm` is best-effort.** The AM is queryable (the startup query/GOTO
fills `am` proactively), but the VM answers the query with an empty ack,
so `vm` only populates **passively** from spontaneous VM broadcasts when
a video source changes — it's often empty until then.

**Robustness**: STATUS_INFO frames are accepted only when the source byte
is a known ML source. The bus carries short stub STATUS_INFO frames
(pl_len=0, where byte 10 is actually the checksum) and frames advertising
transient/non-audio bytes; both used to flicker the source to junk like
`0xe3 -> "?"`. Those are now dropped. `origin` is `rx` (a real device
announced it) or `tx` (our own bridge did).

**Startup source query**: the active source's STATUS_INFO is only
broadcast spontaneously, so right after the daemon starts `state:ml`
would be `Unknown` until the next broadcast. To fill it immediately the
tracker emulates a link-room speaker asking **both masters** what
they're distributing — `REQUEST_DISTRIBUTED_SOURCE` sent `FROM` a link
address (default `0x06`) `TO` the AM (`0xC1`) and the VM (`0xC0`). A
master answers a link device (not the other master) with the source
byte, and this is **non-disruptive** — it's the normal link-join query;
playback keeps going. On a typical AM+VM system the AM returns the audio
source at the reply's `raw[13]`; the VM returns a bare ack here, but it's
queried too so the daemon also works in VM-led / AM-absent topologies.
The query fires a few times at startup and stops as soon as a source is
known.

The `0x08` reply gives the **source** but not the track. To also fetch
the **track/channel**, once the source is known the tracker sends a
`GOTO_SOURCE` for that *same* source — a link-join / re-announce, **not**
a source switch, so it stays non-disruptive (playback continues). The AM
responds by broadcasting a full `STATUS_INFO` (with `CH_TRACK`), which
the listen loop parses. In practice `state:ml` has the source within
~1 s and the track within ~2 s of start.

These are the only telegrams the tracker transmits. Flags:

```
--query-addr 0x06   link-room address to emulate (default 0x06)
--no-query          disable the query entirely (purely passive)
--no-goto           do the source query but skip the GOTO-refresh: keeps
                    it strictly read-only (no phantom link-join), at the
                    cost of no track until the next spontaneous broadcast
```

Use `--no-query` (or a free `--query-addr`) if a real link-room speaker
occupies `0x06`. The GOTO-refresh registers the query address as
momentarily "joined" in the AM's bookkeeping (benign — no audio is
drawn, the source is already playing); `--no-goto` avoids even that if
you want the query to be purely a read.

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
