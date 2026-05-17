# audio-tools

> ## ⚠️ The RIAA script is experimental — for testing only
>
> `capture-phono-riaa.sh` runs the cartridge **raw** into the on-HAT ADC
> and applies the RIAA curve in software. It exists for characterising
> the ADC + DL'80 chain; it is **not** the recommended way to actually
> listen to records with this hardware.
>
> **For normal listening, use the turntable's built-in RIAA preamp** —
> connect its line-level output to any line input (the HAT's DAC line
> input, an external amp, whatever). A proper analogue phono stage
> out-performs a
> 3.3 V σ-Δ ADC fed by a 4 mV cartridge every time, and you skip the
> +32 dB PGA workaround this script needs to keep the signal above the
> chip's idle-noise floor.
>
> The `capture-phono-raw.sh` script is even more obviously a measurement
> tool — flat 0 dB ADC, no filter, no normalisation. Useful for null-
> tests, not for listening.

Two manual test scripts that record one minute of phono audio from a
Beogram turntable on the DL'80 bus and write a FLAC file.

| Script | Pipeline | Use when |
|---|---|---|
| `capture-phono-riaa.sh` | BG.Play → ADC: VIN4 + **+32 dB PGA** → 60 s WAV → **RIAA hp30** → −1 dBFS peak normalize → **FLAC** | Listening / archiving |
| `capture-phono-raw.sh`  | BG.Play → ADC: VIN4 + **0 dB PGA** → 60 s WAV → **FLAC** (no filter, no normalize) | Measurements, null-tests |

Both are self-contained. No installer, no helper files, no service.
Just run them.

## One-time prerequisites

The `mdt-tools` install (run `mdt-tools/install.sh` first) handles the
broker, the DAC+ADC overlay, the boot config, etc. These extra apt
packages are needed *only* for the audio-tools scripts and the install
script does **not** pull them automatically:

```sh
sudo apt install \
    redis-tools alsa-utils i2c-tools ffmpeg \
    python3-numpy python3-scipy python3-soundfile
```

You also need:

* `mdtv2-broker.service` running, with the ATtiny826 HAT physically
  wired to the Beogram's DL'80 line (so `redis-cli PUBLISH
  link:dl80:transmit a9` actually reaches the turntable).
* DAC+ADC overlay loaded (`dtoverlay=hifiberry-dacplusadc`,
  `dtparam=audio=off` — both come from `mdt-tools/install.sh`; the
  HAT we ship is electrically compatible with that kernel driver).
* Beogram wired into VIN4 of the HAT (L+/R+ to VINL4/VINR4; L−/R−/shield
  to AGND at the turntable end).

## Use

```sh
# RIAA-corrected, listening-ready FLAC:
sudo ./capture-phono-riaa.sh mytrack
# -> mytrack.flac

# Raw flat-ADC FLAC, no PGA boost, no RIAA:
sudo ./capture-phono-raw.sh mytrack-raw
# -> mytrack-raw.flac
```

Without an argument the basename defaults to a timestamp
(`phono-YYYYMMDD-HHMMSS.flac` / `phono-raw-…`).

## Env overrides

| Var | Default | Notes |
|---|---|---|
| `DUR` | `60` | Recording length in seconds |
| `SPINUP_S` | `10` | Wait between BG.Play and `arecord`. ~8 s is the minimum on a Beogram 5500; 10 s gives margin |
| `AUDIO_DEV` | `hw:sndrpihifiberry,0` | ALSA capture device |
| `I2C_BUS` | `1` | Pi I²C bus |
| `ADDR` | `0x4a` | PCM1862 address |

## DL'80 controls (verified on Beogram 5500)

```sh
redis-cli PUBLISH link:dl80:transmit a9     # play (start the platter)
redis-cli PUBLISH link:dl80:transmit 95     # next track
redis-cli PUBLISH link:dl80:transmit f3     # previous track
redis-cli PUBLISH link:dl80:transmit cb     # stop / standby
```

## Why these settings

* **+32 dB PGA + RIAA in software**: the PCM1862 is a Σ∆ ADC; idle-noise
  artefacts are audible when the input sits near the noise floor. An MMC
  cartridge at 0 dB PGA peaks around −42 dBFS — well inside that
  region. +32 dB lifts it to ≈ −14 dBFS and the artefacts mask. RIAA is
  applied offline so the curve can be iterated in code.
* **hp30 RIAA variant**: 4th-order Butterworth HP at 30 Hz, then the
  standard RIAA curve (3180 / 318 / 75 µs). Cuts subsonic warp and
  motor energy that RIAA's +17 dB at 50 Hz would otherwise amplify into
  IM-distortion territory. Audibly the cleanest of the variants we tried.
* **Raw script keeps PGA at 0 dB and skips RIAA**: that's the "honest"
  measurement chain. Useful for null-tests against the corrected one,
  or for capturing a flat WAV that you want to apply a different curve
  to later.
