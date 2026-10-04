# ha-notifier

Forwards what ml-source-bridge is playing to a Home Assistant webhook, so HA
can show streams that don't come from Music Assistant -- e.g. AirPlay
straight from an iPhone.

```
providers (shairport / sendspin / mpd / turntable)
      │  polled by ml-source-bridge
      ▼
redis  state:nowplaying (hash) + link:ml:nowplaying (pub/sub)
      │
      ▼
ha-notifier ──HTTP POST──► HA webhook ──► sensor.mdt_n_music
```

Payload and redis keys: see `ml-source-bridge/README.md`, "Now playing on
redis". HA side: `home-assistant/` (webhook sensor, templates, card).

## Configuration

`[ha_notifier]` in `/etc/ml-source-bridge.toml` -- the commented block in
`ml-source-bridge/config.toml.example` lists every option. Minimal:

```toml
[ha_notifier]
enabled = true
url     = "http://homeassistant.local:8123/api/webhook/<webhook_id>"
sources = ["N.MUSIC"]
```

Then `sudo systemctl restart ha-notifier.service` -- or set it in the web
UI, which restarts it for you. With `enabled = false` (or no section) the
service idles until the config changes.

## Behaviour

* **Startup:** sends the current state of every selected source once.
* **Debounce:** a change goes out once it has been stable for `debounce_ms`
  (300 ms); leaving `playing` waits `idle_debounce_ms` (3 s) so the brief
  `Paused` shairport-sync reports while the ML system wakes doesn't make the
  HA card flicker.
* **Duplicates** are not sent again, except every `keepalive_s` (300 s) so
  HA catches up after a restart -- trigger sensors only change on a POST.
* **HA unreachable:** logged, retried every `retry_s` (30 s), never fatal.
* **Cover art:** the bridge reports the cover as a file on the Pi
  (shairport-sync's cover cache). ha-notifier serves exactly those files on
  `cover_port` (8099) and sends HA a `cover_url` like
  `http://masterlink-bridge.local:8099/cover/<id>.jpg`; the browser showing
  the HA card loads it from there. Works when HA is opened over plain http
  in the LAN -- an https page would block it as mixed content. The service
  user (`mdt`) must be able to read the cover cache.

## Logs

```sh
sudo journalctl -u ha-notifier.service -f
redis-cli SUBSCRIBE link:ml:nowplaying      # what the bridge publishes
```
