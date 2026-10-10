# ml-source-bridge

Pretends to be a Bang & Olufsen Source Center (SC, address 0xC2) on the
MasterLink bus, so that another audio source -- AirPlay via shairport-sync,
or a DL'80 turntable -- appears to a real BeoSystem / BeoCenter as N.RADIO /
N.MUSIC / etc.

Architecture:

```
shairport-sync (AirPlay receiver)   |   DL'80 Beogram (turntable)
        │                          |          │  opcodes + analogue audio
        ▼                          |          ▼
    provider                <-- providers/airplay.py   D-Bus → metadata + control
                            <-- providers/turntable.py DL'80 + ADC→DAC loopback
        │
        ▼
   role = sc                <-- roles/source_center.py spoofs SC handshake
        │
        ▼     publish hex telegrams
    Redis  ────────────►  mdtv2-broker  ────────────►  MDT MCU   ────►  ML bus
        ▲     subscribe to RX                                              │
        │                                                                  ▼
        └──────── RX hex telegrams ◄───────────────────────────────  ML devices
```

## Providers

| provider | backend | audio path |
|---|---|---|
| `airplay` | shairport-sync over D-Bus | shairport feeds the DAC itself |
| `turntable` | DL'80 Beogram over the Datalink wire | we loop ADC -> DAC ourselves |

### `turntable` -- a Beogram as an ML source

Set `provider = "turntable"` on whichever source byte you want to give up
(N.RADIO `0xa1` is the usual choice) and configure the `[turntable]` section.
It is not the default for any source.

Selecting that source on your Beo remote spins the record; Step Up / Step
Down skip tracks; leaving the source parks the arm. ML transport events
become single-byte DL'80 opcodes (`0xA9` play, `0x95` next, `0xF3` prev,
`0xCB` standby -- all overridable per Beogram model), and because a
turntable is analogue we also move the audio: ADC capture -> optional
software RIAA -> DAC playback, both ends being the same sound card, so
one clock domain and no drift.

The B&O display gets a fixed title where a track name would normally go --
`PHONO` by default, changeable with `metadata_title` (or `""` to leave it
blank). A record player has nothing else to report, and the metadata pump
only fires on *change*, so the claim handshake now pushes the provider's own
metadata instead of its `"Connecting"` placeholder -- otherwise a constant
title would only ever appear once per bridge start.

**The deck's own buttons drive ML too.** It reports state on DL'80, so
pressing PLAY on the turntable switches the ML system to this source (via
the role's auto-wake), and pressing STANDBY releases the source and sends a
Beo4 STANDBY so the system switches off -- `standby_ml_on_stop = false` if
you'd rather it only dropped the source. Expect ~5 s between pressing
STANDBY and ML going off: the stream watcher debounces five polls before it
believes a stop. A deck that started on its own is also never re-cued -- the
bridge skips `BG.Play` when it knows the platter is already turning, so the
arm doesn't jump back to track 1.

Two hardware gotchas, both handled but worth knowing:

* **The ADC input mux.** The PCM1862 powers up on VIN1; the phono input is
  VIN4. `adc_setup = true` (the default) points it at VIN4 over I2C -- skip
  that and you get a spinning record with dead silence. Needs `i2c-tools`
  plus the `i2c-dev` module; `install.sh` provisions both.
* **Status reporting is deck-dependent.** Decks that emit DL'80 status
  (`0xC3` playing / `0xCE` stopped) let the bridge notice a hand-started
  record and wake ML to it. A Beogram 5500 answers only `0xFC`/`0xF1`,
  which aren't state, so there the bridge follows its own commands only.

#### Feed it line level

The supported hookup is **line level into the ADC with `pga_db = 0` and
`riaa = false`** -- either a turntable with its own RIAA preamp (most
Beograms) or a bare cartridge through an **external phono preamp**.

> **`riaa = true` + high `pga_db` is a development path with poor sound
> quality -- not for listening.** It applies the playback curve in software
> (measured +13.09 dB @ 100 Hz, 0.00 dB @ 1 kHz, ~1.4 dB over-cut at
> 10 kHz), which is accurate enough to characterise the ADC and experiment
> with curves. But a ~4 mV cartridge straight into a 3.3 V sigma-delta ADC
> plus ~36 dB of make-up gain is thin, noisy and short on headroom; no
> amount of DSP fixes that. If your deck has no preamp, buy an external
> one -- don't reach for this.

Both are off by default. Note they only work as a pair: a bare cartridge
needs the gain *and* the curve, so enabling one without the other gives
either silence or a wrong tonal balance.

#### Music recognition (optional)

With `recognize = true` in `[turntable]` the bridge identifies the record
via Shazam and shows title / artist / album on the B&O display; the cover
goes wherever now-playing goes (e.g. the Home Assistant card). It works on
the audio alone -- no Datalink cable needed:

* `audio_tap.py` sits in the ADC -> DAC loopback, forwards every byte
  unchanged, and hands a copy to a worker process (dropped, never delayed,
  if the worker falls behind).
* The worker detects new tracks from silent gaps (below -50 dBFS for
  1.5 s -- the groove between tracks, a record change), then asks Shazam
  about ~10 s of the track (`phono_recognize.py`, in a helper process).
* A result is shown only when two attempts ~10 s apart agree -- rare
  tracks occasionally produce one-off false matches. No match: retries
  with backoff; the display keeps `metadata_title`. Edition suffixes like
  "(2016 Remaster)" are stripped from album names.
* Results travel over redis pub/sub (`link:phono:recognized`); nothing is
  written to disk.
* On the bus the texts go out as EXTENDED_SOURCE_INFORMATION (capped at 10
  characters). Whether a panel shows them depends on the model: a
  BeoSound 3200 keeps showing only the source name (`display_name`), so
  there the result is mainly useful via now-playing (Home Assistant).

In tests with recordings from a Beogram 7000, mainstream pop/rock was
recognized in every snippet (shown ~20 s after a track starts), while a
jazz record wasn't in Shazam's catalog and produced one false match that
the agreement rule filters out. Shazam's coverage decides.

shazamio is an **unofficial** Shazam client and can break when Shazam
changes its API. `install.sh` installs it into
`/opt/mdt-tools/.recognize-venv` only when `recognize = true`, so enable it
first, then re-run `install.sh`. The ha-notifier forwards only the sources
in `[ha_notifier] sources` -- the turntable's source isn't in the shipped
HA setup.

#### Music recognition on the bus (`ml_listen`)

The HAT's ADC (PCM1862) doesn't only have the phono input: VIN1 and VIN2
carry the MasterLink audio lines. Measured with a BeoSound 3200 as audio
master, both at about -30 dBFS (RMS) for a CD as for N.MUSIC; the DataLink
(VIN3) and phono (VIN4) inputs stay silent then. So the bridge can listen
to what the bus plays and identify it, also for sources it doesn't provide:

```toml
[ml_listen]
enabled = true
# sources = ["CD", "A.MEM"]   # display names or source bytes; omit = all
```

How it works (`core/ml_listen.py`):

* It follows the state tracker (`link:ml:state`). While the audio master
  reports Playing a source that isn't one of this bridge's own (those know
  what they play) -- and, if `sources` is set, is in that list -- it
  records from the ADC: `arecord | audio_tap.py --recognize`, nothing goes
  back to the DAC.
* Same recognizer as the turntable: ~10 s snippets, a result counts once
  two attempts agree, new track on a silent gap. On a CD the audio master's
  track number also starts a new track (`SIGUSR1` to the tap), and a
  recognized title stays until that number changes -- the recognizer alone
  would clear it after two misses, which quiet passages cause.
* Results go out like a source of our own: `state:nowplaying` /
  `link:ml:nowplaying`, keyed by the bus source's byte, every message with
  `"origin": "ml_listen"` and the track number in `"track"`. ha-notifier
  forwards them when `[ha_notifier] sources` contains `"ml_listen"` (or the
  source's name); HA shows them via `sensor.mdt_ml_listen`.
* **Silence = idle:** the state tracker can miss a stop -- a source-less
  RELEASE (e.g. from a bridge shutting down) stops the audio master's
  source without it being reported. So after 20 s of silence (the tap's gap
  detector) the listener reports the source idle while it keeps listening,
  and playing again when the music returns.
* **Original album** (`core/musicbrainz.py`, `album_lookup = true` by
  default): Shazam often names a compilation instead of the album a track
  comes from. After each recognition the listener looks the recording up on
  MusicBrainz -- by ISRC if Shazam reports one, else by title and first
  artist -- and takes the release that is an Album without secondary type
  (no Compilation, Live, ...), preferring the one with the track at the
  audio master's track number, else the earliest. The cover then comes from
  the Cover Art Archive, if it has one. A new recognition is held back for
  up to 6 s for this, so HA doesn't first show the compilation. No match or
  no network: Shazam's album stays.
* **ADC sharing** (`core/adc.py`): only one process can record. The
  turntable has priority -- when it starts its loopback, the listener stops
  first and resumes afterwards. Each sets the ADC input mux to its own input
  on start (`adc_input_reg`, default `0x41` = VIN1).

Tested on a BeoSound 3200 with a CD: recognized ~30 s after a track change
(two attempts), sent on to HA with cover; Shazam's "Peaceful Choral Music"
corrected to "Officium" via MusicBrainz. Known limits:

* The album lookup depends on MusicBrainz's data; obscure releases may not
  be there (then Shazam's album stays).
* The listener only hears what the audio master puts on the ML audio lines.
* Same shazamio caveats as above; the venv is installed when
  `[ml_listen] enabled = true`, so re-run `install.sh` after enabling.

## Auto-wake

When a provider's stream starts, the SC injects a virtual Beo4 keypress so
the system switches to that source. Two things bound it:

* **It is skipped when the bus is already on our source.** The stream
  starting is not itself a reason to change what the house is doing. If the
  user selected the source on a remote and the stream only arrived
  afterwards, waking would pull the wake target into a session it was never
  part of -- in a multi-room house the VM is the main-room TV, so the main
  room switches itself on while you are listening somewhere else. Known
  state comes from our own grant bookkeeping, cross-checked against
  `state:ml` from the state-tracker daemon so it survives a bridge restart
  mid-session (optional -- absent key just falls back to our own view).
* **`wake_target`** picks the address: `"vm"` (default, mirrors a real
  Source Center), `"am"`, `"off"`, or any ML address such as `0x06` to aim
  it at a single link node. Only the VM and AM forms are capture-verified.

## Now playing on redis

The bridge publishes what each configured source is playing -- which
provider owns it, playback state and track metadata -- so other
processes can follow it without talking D-Bus / MPD themselves (e.g. to
show a direct AirPlay stream from an iPhone in Home Assistant). Runs for
both roles, independent of `auto_wake`; polled once a second, published
only on change.

```sh
redis-cli HGETALL state:nowplaying          # current view, field = source byte
redis-cli SUBSCRIBE link:ml:nowplaying      # one message per change
```

Payload (one source):

```json
{"source": "N.MUSIC", "source_byte": "0x7a",
 "provider": "airplay", "display": "Apple Music",
 "state": "playing", "title": "Theme from Harry's Game",
 "artist": "Clannad", "album": "Magical Ring",
 "art_url": "file:///tmp/shairport-sync/.cache/coverart/cover-….jpg"}
```

* `state` -- `playing`, `paused` (session held, e.g. paused on the iPhone)
  or `idle` (AirPlay client gone, MA / MPD stopped, bridge shut down).
  When `idle`, `provider`, `display` and the metadata fields are empty.
  The turntable never reports `paused`.
* `display` -- the provider's `[provider_displays]` entry, else the
  source's `display_name`.
* `art_url` -- cover art as the backend reports it (MPRIS `mpris:artUrl`):
  for AirPlay a file in shairport-sync's `cover_art_cache_directory`, for
  Sendspin whatever it puts there; empty for MPD and the turntable.
* On a multi-provider source, a paused sub keeps the source until another
  sub starts playing.
* No timestamp: the same state always serialises to the same string, so
  consumers can drop duplicates by comparison.

## Configuration

`/etc/ml-source-bridge.toml` -- copied from `config.toml.example` on first
install; that file documents every option. Key fields:

* `role` -- `sc` (source center) or `am` (audio master)
* `[[sources]]` -- one table per ML source byte: `source_byte` (e.g. `0xA1`
  = N.RADIO), `display_name` (shown on B&O panels), `provider` (`airplay`,
  `sendspin`, `mpd`, `turntable`, or a list of them -- see multi-stream)
* `[provider_displays]`, `[mpd]`, `[turntable]`, `[light_handler]`,
  `[ha_notifier]` -- per feature

Examples for each: [main README, Configuration](../README.md#configuration).

## Checking a config

```sh
python3 ml_source_bridge.py --config /etc/ml-source-bridge.toml --check-config
```

Runs the same checks as startup (sources, providers, wake target, LIGHT
keys) without touching the bus or any backend; prints the problems and exits
1, or exits 0. The web UI runs it before every save. The bridge also exits
cleanly when `ml-source-bridge` (or `all`) is published on redis
`link:ctl:restart` -- systemd starts it again with the new config.

## Logs

Goes to `/tmp/mdt.log` (shared with the broker) and via journald.
Raw ML traffic also visible via `ml-debug/`.
