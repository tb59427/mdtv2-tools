# Getting started: MDTV2, BeoControl and BeoBar

Three projects that together bring a B&O MasterLink system up to date:
streaming services on the B&O remote, Home Assistant, and the Mac. This page
says what to install where; the details are in each project's docs.

| Piece | Runs on | What it does |
|---|---|---|
| **MDTV2 HAT + mdtv2-tools** (this repo) | Raspberry Pi on the ML bus | Streams (Music Assistant via Sendspin, AirPlay, MPD) as a B&O source, e.g. N.MUSIC; turntable as a source; music recognition (record and CD); MasterLink Gateway emulation for Home Assistant; now-playing to Home Assistant; web UI |
| **[BeoControl](https://github.com/wolfgangschneider/BeoControl)** (Wolfgang Schneider) | Mac or Windows with a BeoLink PC2 | The computer as a room on the ML bus: speakers on the PC2, Beo4 control, the computer's own sound or any ML source |
| **[BeoBar](https://github.com/tb59427/BeoBar)** | Mac | BeoControl in the menu bar: Beo4-style remote, music makes way for calls, keyboard media keys |
| Home Assistant (optional) | anywhere in the LAN | Dashboard card, automations on Beo4 keys, players for every room |

You can use each piece on its own: the Pi doesn't need a PC2, and BeoControl
doesn't need the Pi.

## 1. The Pi with the MDTV2 HAT

**Hardware:** a Raspberry Pi with the MDTV2 HAT from
[masterdatatool](https://gitlab.com/masterdatatool/software/mdtv2-tools)
(Philip Voigt), connected to the MasterLink bus (and DataLink, for a DL
turntable or music system).

**Software:** on a fresh Raspberry Pi OS Lite, one line:

```sh
curl -sSL https://raw.githubusercontent.com/tb59427/mdtv2-tools/master/bootstrap.sh | sudo bash
```

Reboot if the installer says `REBOOT REQUIRED`. Then:

1. **Web UI:** add a `[web]` section with a password to
   `/etc/ml-source-bridge.toml`, `sudo systemctl restart mdt-web`, open
   `http://<pi>.local/` ([web-ui.md](web-ui.md)). Everything below can be set
   there.
2. **Sources:** which B&O source the Pi provides (e.g. N.MUSIC) and from
   what -- Music Assistant (Sendspin), AirPlay, MPD, several at once
   ([providers.md](providers.md)).
3. **Optional:**
   - Turntable as a source and **music recognition** -- for the record
     (`[turntable] recognize`) and for what else the bus plays, e.g. a CD
     (`[ml_listen]`). Re-run the installer after switching either on, so it
     installs shazamio ([README](../README.md#turntable-and-music-recognition)).
   - **Home Assistant:** the MasterLink Gateway emulation replaces a B&O MLGW
     for HA's mlgw integration ([mlgw-emulation.md](mlgw-emulation.md)), and
     now-playing goes to a webhook with a ready-made card
     ([home-assistant.md](home-assistant.md)).

Updating later: run the same `curl … | sudo bash` line again.

## 2. A Mac with a BeoLink PC2: BeoControl and BeoBar

**Needs:** macOS 14+, a BeoLink PC2 on USB, Homebrew, the
[.NET 10 SDK](https://dotnet.microsoft.com/download) and the Xcode Command
Line Tools (`xcode-select --install`).

```sh
brew install libusb

# BeoControl server -- the beobar branch has the changes BeoBar needs,
# until they're merged upstream
git clone -b beobar https://github.com/tb59427/BeoControl.git
cd BeoControl
dotnet publish UI/BeoControlBlazor/BeoControlBlazorServer/BeoControlBlazor.csproj \
  -c Release -f net10.0 -o ~/BeoControlServer
cd ..

# BeoBar
git clone https://github.com/tb59427/BeoBar.git
cd BeoBar
./build.sh
ditto ~/Library/Caches/BeoBar-build/BeoBar.app /Applications/BeoBar.app
open /Applications/BeoBar.app
```

Then:

1. Right-click the B&O icon in the menu bar → *Web-Oberfläche im Browser* →
   connect the PC2 once in BeoControl.
2. Allow BeoBar under *System Settings → Privacy & Security →
   Accessibility*, for the keyboard media keys.
3. Gear in the remote's display: set up the source keys, and which audio
   output feeds the PC2 (for switching to the Mac during calls).

Details: [BeoBar README](https://github.com/tb59427/BeoBar#readme).

**Windows:** BeoControl has installers in its
[releases](https://github.com/wolfgangschneider/BeoControl/releases). BeoBar
is macOS only.

## 3. Home Assistant (optional)

- **mlgw integration** (HACS) pointing at the Pi instead of a B&O MLGW: one
  media player per room, Beo4 keys as events
  ([mlgw-emulation.md](mlgw-emulation.md)). Adding the PC2 room in the Pi's
  web UI makes it a player too -- switch on, volume and sources from HA reach
  BeoControl.
- **Now playing:** webhook sensors and one button-card for N.MUSIC, the
  turntable and recognized CDs
  ([home-assistant.md](home-assistant.md)).
