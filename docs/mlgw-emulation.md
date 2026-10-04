# MasterLink Gateway emulation (mlgw-emu)

The Pi can take the place of a B&O MasterLink Gateway (MLGW) for Home
Assistant's [mlgw integration](https://github.com/giachello/mlgw) — the HA
side stays unchanged: same integration, same `mlgw.ML_telegram` /
`mlgw.MLGW_telegram` events, same media players.

## What is emulated

The integration talks to an MLGW over three interfaces; the Pi provides all
three from its view of the bus:

| Interface | Port | Served by | What |
|---|---|---|---|
| HTTP `/mlgwpservices.json` | 80 | mdt-web | Rooms, devices (MLN) and their sources |
| MLGW protocol | 9000 | mlgw-emu | Login, Beo4 commands to a device, the MLGW's events |
| Telnet `_MLLOG` | 23 | mlgw-emu | Every ML telegram → `mlgw.ML_telegram` events |

MLGW events, derived from the bus as a real MLGW reports them (checked side
by side against one):

- **LIGHT/CONTROL** — every key a master forwards to the MLGW address, incl.
  LIGHT itself and Key Release; room = the forwarding device's room. Only
  masters that support LIGHT forward (a BeoVision 10 does; a BeoVision 6 or
  BeoLab 2000 doesn't).
- **All standby** — RELEASE / STANDBY from a master to all devices.
- **Source status, picture & sound status** — what masters send to the MLGW
  address (e.g. the video master's source, volume, speaker mode). Like a real
  MLGW, link rooms aren't reported; the integration tracks them from the ML
  log. `extended_status = true` adds them anyway.

Not emulated: virtual buttons / macros, BeoRemote One commands, XMPP /
zeroconf discovery (add the integration by IP), anything else that uses an
MLGW (e.g. the BeoLink app).

## Set up

1. Enable the web UI ([web-ui.md](web-ui.md)) — HA fetches the device list
   from port 80.
2. In the UI, tab **MasterLink Gateway**: tick *Enabled*, keep *Listen only*
   while a real MLGW is still on the bus, set login and password, Save.
3. **Devices**: *Import MLGW export…* with your MLGW's `mlgwpservices.json`
   (download it from the MLGW:
   `curl --digest -u <user> http://<mlgw>/mlgwpservices.json -o mlgwpservices.json`),
   or add rooms and devices by hand.
4. Give every device its **ML address** — pick it from the devices seen on
   the bus, or press *Identify* and any Beo4 key in that room. To read the
   addresses a real MLGW uses: while it is on the bus, run
   `python3 /opt/mdt-tools/ml-debug/ml_debug.py --rx-only` and reload the
   mlgw integration in HA — the MLGW sends each device a "Light Timeout",
   in MLN order (MLN 1 first); the TO address of each is that device's.
5. *Save devices*.

## Try it without touching your Home Assistant

Never add the emulation as a second mlgw entry to your production HA: every
`mlgw.ML_telegram` would fire twice, and automations run twice. Use a
separate HA instead, e.g. in Docker on a laptop:

```sh
mkdir -p ~/ha-test/config/custom_components
cp -R mlgw/custom_components/mlgw ~/ha-test/config/custom_components/   # from a clone of the integration
docker run -d --name ha-test -p 8123:8123 -v ~/ha-test/config:/config ghcr.io/home-assistant/home-assistant:stable
```

Add the integration there with the Pi's IP and the emulation's login, with
"Use undocumented MasterLink bus feature" ticked. *Listen only* keeps HA's
commands off the bus; to test them, unplug the real MLGW from the bus first
— both would use the MLGW address 0xF0 — and untick *Listen only*.

## Switch your Home Assistant over

1. Keep the real MLGW's **serial** (the import does; `serial` in `[mlgw]`
   overrides it) — HA's entity ids are built from serial + MLN, so entities
   and automations carry over.
2. Unplug the real MLGW (bus and network).
3. UI: untick *Listen only*, Save.
4. HA: delete the mlgw integration, add it again with the **Pi's IP** and the
   emulation's login, "Use undocumented MasterLink bus feature" ticked. HA
   restores entity names and settings when the same unique ids come back.
5. Check: media players show their state, a Beo4 key fires
   `mlgw.ML_telegram`, LIGHT in a room that forwards it fires
   `light_control_event`.

Back: plug the MLGW in, tick *Listen only* again, point the integration at
the MLGW again.

## Known issues of the integration

The integration learns each device's ML address at startup by sending it a
"Light Timeout" and matching the answers **in arrival order**; HA handles
them on several threads. Answers close together can get shuffled, and on a
reload the old instance's telnet session can deliver one twice — devices get
each other's addresses, or the integration raises an exception and media
players lose their state until the next reload. This happens with a real
MLGW too. The emulation works around it: it spaces its telegrams 150 ms
apart and closes a host's older telnet session when it logs in again.

## Logs

```sh
sudo journalctl -u mlgw-emu -f
redis-cli GET state:mlgw            # HA connections, listen-only
```
