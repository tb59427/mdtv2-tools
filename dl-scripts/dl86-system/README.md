# dl86-system

Manual test script for capturing audio from a DL'86 music system's CD
source through the on-HAT PCM5122/PCM1862 DAC+ADC.

| Script | Pipeline |
|---|---|
| `capture-cd.sh` | DL'86 CD-on (`17:00C920`) → wait 5 s → ADC: VIN3 + 0 dB PGA → 60 s WAV → DL'86 standby (`17:008600`) → FLAC |

## Wiring

CD player line-out (or BM tape-record-out, depending on your music
system topology) wired to the HAT's **VIN3 single-ended** (AIN3).
Phono / VIN4 stays free for a turntable on the same HAT.

## One-time prerequisites

```sh
sudo apt install -y \
    redis-tools alsa-utils i2c-tools ffmpeg
```

You also need:

* `mdtv2-broker.service` running, with the MDT HAT physically
  wired to the music system's DL'86 line.
* DAC+ADC overlay loaded (handled by `mdt-tools/install.sh`; the
  HAT is electrically compatible with the `hifiberry-dacplusadc`
  kernel driver).
* DL'86 commands verified for your specific music system — see
  `dl-docs/dl86-music-system/music-system.md`. The script's CD-on /
  standby bytes match what we tested; if your system uses a different
  format, edit the two `redis-cli PUBLISH` lines in `capture-cd.sh`.

## Use

```sh
sudo ./capture-cd.sh                  # -> cd-YYYYMMDD-HHMMSS.flac
sudo ./capture-cd.sh my-album-side1   # -> my-album-side1.flac
```

## Env overrides

| Var | Default | Notes |
|---|---|---|
| `DUR` | `60` | Recording length in seconds |
| `SPINUP_S` | `5` | Wait between sending CD-on and starting `arecord`. CD source-switch is fast; 5 s gives margin |
| `AUDIO_DEV` | `hw:sndrpihifiberry,0` | ALSA capture device |
| `I2C_BUS` | `1` | Pi I²C bus |
| `ADDR` | `0x4a` | PCM1862 address |
| `SKIP_STANDBY` | `0` | Set to `1` to leave the system on after the recording finishes (default: standby) |

## Why these settings

* **VIN3 + 0 dB PGA**: CD output is line-level (~2 Vrms ≈ +6 dBV peak),
  about 600× higher than an MMC cartridge. The PCM1862's ~2.1 Vrms
  full-scale-input window puts CD peaks just below clipping at 0 dB
  PGA. Applying any positive gain would clip; if you see clipping in
  practice, drop to negative dB (e.g. `0xC8` for −12 dB on registers
  `0x01`/`0x02`).
* **No software RIAA, no normalization, no high-pass**. CD output is
  already flat line-level. The recording is exactly what the ADC
  digitised.
* **48 kHz, S32_LE, 2-channel**. Same as the phono scripts; matches the
  HAT's native rate so no resampling.
* **Standby at the end**. Most B&O systems wake their CD transport on
  source-switch and stay engaged; sending `0x0C` (Beo4 STANDBY) is the
  clean way to end the session. Pass `SKIP_STANDBY=1` to keep playing
  for back-to-back captures.

## Stopping manually

```sh
redis-cli PUBLISH link:dl86:transmit 17:008600     # standby
```
