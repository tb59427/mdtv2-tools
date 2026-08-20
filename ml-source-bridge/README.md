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

## Configuration

`/etc/ml-source-bridge.toml` -- copied from `config.toml.example` on first
install. Key fields:

* `role` -- `sc` (source center) or `am` (audio master)
* `[[sources]]` -- one block per provider. For each: `byte` (ML source byte,
  e.g. `0xa1` = N.RADIO), `display_name` (12-char string shown on B&O
  displays), `provider` (`airplay` etc.).

## Logs

Goes to `/tmp/mdt.log` (shared with the broker) and via journald.
Raw ML traffic also visible via `ml-debug/`.
