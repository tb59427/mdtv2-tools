# system-config

Raspberry Pi system-level configuration applied by the top-level
`install.sh`. Nothing in here is a service or a runtime artefact — these
files are templates / snippets the installer copies into place.

## `boot-config.txt.snippet`

Appended to `/boot/firmware/config.txt` (or `/boot/config.txt` on older
images) on first install. Contents:

```
enable_uart=1                   # PL011 on PA1/PA2 -> /dev/serial0
dtoverlay=disable-bt            # bluetooth doesn't get to take ttyAMA0
dtparam=audio=off               # turns off the on-SoC HDMI/3.5mm sound
dtoverlay=hifiberry-dacplusadc  # HiFiBerry DAC+ADC HAT (PCM5122 + PCM1862)
```

The installer wraps the snippet between `# --- mdt-tools begin ---` and
`# --- mdt-tools end ---` markers and only adds it if those markers are
not already present. It never removes them on later runs, so manual edits
inside the block are safe.

If you want to remove the mdt-tools modifications, delete the block by
hand and reboot.
