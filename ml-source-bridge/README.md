# ml-source-bridge

Pretends to be a Bang & Olufsen Source Center (SC, address 0xC2) on the
MasterLink bus, so that a modern audio source (currently AirPlay via
shairport-sync) appears to a real BeoSystem / BeoCenter as N.RADIO /
N.MUSIC / etc.

Architecture:

```
shairport-sync (AirPlay receiver)
        │
        ▼
    provider                <-- providers/airplay.py   D-Bus → metadata + control
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
