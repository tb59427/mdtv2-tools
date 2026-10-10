# Testing changes on the Pi without touching the installed version

`install.sh` overwrites `/opt/mdt-tools/` and the systemd units. To try a
work-in-progress checkout while keeping the installed version as a fallback,
run it as a **test instance** next to it: stop the installed services,
start the checkout's versions as transient systemd units, and go back with
two commands -- or a reboot.

Broker and state-tracker keep running; the bridge, ha-notifier, mlgw-emu
and mdt-web are swapped. Examples use `pi@masterlink-bridge` and a checkout
on a Mac.

## One-time setup (on the Pi)

```sh
sudo install -d -o "$USER" -g "$USER" -m 755 /opt/mdt-tools-test
sudo cp /etc/ml-source-bridge.toml /opt/mdt-tools-test/test.toml
sudo chown "$USER":mdt /opt/mdt-tools-test/test.toml
sudo chmod 660 /opt/mdt-tools-test/test.toml   # mdt-web saves it
nano /opt/mdt-tools-test/test.toml      # e.g. add [web] with state_dir below
sudo install -d -o "$USER" -g mdt -m 2770 /opt/mdt-tools-test/web-state /opt/mdt-tools-test/mlgw-state
```

For a test web UI, point `[web] state_dir` at `/opt/mdt-tools-test/web-state`,
`[mlgw] devices_file` at `/opt/mdt-tools-test/mlgw-state/devices.json`, and
list the test units in `[web] units` so the status page shows them.

The directory belongs to your login user, so copying needs no sudo; the
services run as `mdt`, which reads the code through the world bits and the
config through its group. **Keep `test.toml` in group `mdt`:** your login
user isn't in that group, so `sed -i`, `cp -p` or an editor that replaces the
file instead of writing it in place silently changes the group to yours --
then the services can't read it. `nano` writes in place; otherwise fix it
with `sudo chgrp mdt test.toml`. Check the config parses:

```sh
sudo -u mdt python3 -c "import tomllib; tomllib.load(open('/opt/mdt-tools-test/test.toml','rb')); print('ok')"
```

## Copy the checkout (from the dev machine, after every change)

```sh
rsync -av --delete --exclude .git --exclude __pycache__ --exclude .DS_Store \
  ./ pi@masterlink-bridge:/opt/mdt-tools-test/repo/ \
&& ssh pi@masterlink-bridge 'chmod -R u=rwX,go=rX /opt/mdt-tools-test/repo'
```

- The code goes into `repo/` so `--delete` can't remove `test.toml` next to
  it.
- The `chmod` matters: `rsync -a` copies the source permissions, and on some
  volumes (exFAT, network shares) everything is `rwx------` -- then `mdt`
  can't read the files. macOS's `openrsync` accepts `--chmod` but ignores it,
  hence the separate `chmod`.

## Switch to the test instance

```sh
sudo systemctl stop ml-source-bridge ha-notifier mlgw-emu mdt-web

T=/opt/mdt-tools-test; R=$T/repo
sudo systemd-run --unit=mdt-bridge-test -p Restart=always -p RestartSec=2 \
  -p User=mdt -p Group=audio -p SupplementaryGroups=mdt \
  /usr/bin/python3 -u $R/ml-source-bridge/ml_source_bridge.py --config $T/test.toml --log-file ""

sudo systemd-run --unit=mdt-notifier-test -p Restart=on-failure -p RestartSec=2 -p User=mdt \
  /usr/bin/python3 -u $R/ha-notifier/ha_notifier.py --config $T/test.toml

sudo systemd-run --unit=mdt-mlgw-test -p Restart=always -p RestartSec=2 -p User=mdt -p Group=mdt \
  -p AmbientCapabilities=CAP_NET_BIND_SERVICE \
  /usr/bin/python3 -u $R/mlgw-emu/mlgw_emu.py --config $T/test.toml

sudo systemd-run --unit=mdt-web-test -p Restart=always -p RestartSec=2 -p User=mdt -p Group=mdt \
  -p AmbientCapabilities=CAP_NET_BIND_SERVICE \
  /usr/bin/python3 -u $R/web/mdt_web.py --config $T/test.toml

sleep 5; systemctl is-active mdt-bridge-test mdt-notifier-test mdt-mlgw-test mdt-web-test
# expected: active active active active
```

- The `Restart=` settings match the real units: after a save in the web UI
  the affected services exit and must come back by themselves.
- `Group=audio` (as in the real unit) replaces `mdt`'s own group;
  `SupplementaryGroups=mdt` keeps it so the bridge can read `test.toml`.
- `CAP_NET_BIND_SERVICE` lets mlgw-emu (telnet, port 23) and mdt-web (port
  80) bind their ports without root.
- `--log-file ""` keeps the test bridge out of `/tmp/mdt.log`, which the
  installed services share.
- **Never run both bridges at once** -- they claim the same ML address.
- **Stopping a bridge stops the music:** on shutdown the bridge sends a
  RELEASE to all devices, and the audio master stops whatever it plays --
  also a CD. Start it again after switching.

Logs and state:

```sh
sudo journalctl -u mdt-bridge-test -u mdt-notifier-test -u mdt-mlgw-test -u mdt-web-test -f
redis-cli SUBSCRIBE link:ml:nowplaying
```

After copying new code: `sudo systemctl restart mdt-bridge-test mdt-notifier-test mdt-mlgw-test mdt-web-test`.

## Back to the installed version

```sh
sudo systemctl stop mdt-bridge-test mdt-notifier-test mdt-mlgw-test mdt-web-test
sudo systemctl start ml-source-bridge ha-notifier mlgw-emu mdt-web
```

A reboot does the same: transient units don't survive it.

## When a test unit fails

```sh
systemctl status mdt-bridge-test --no-pager -n 20 -l
```

| Message | Cause |
|---|---|
| `can't open file …: No such file or directory` | Code not copied to `/opt/mdt-tools-test/repo/` |
| `… Permission denied` on a `.py` file | Permissions from the dev machine -- run the `chmod` above |
| `PermissionError … test.toml` | Bridge started without `SupplementaryGroups=mdt` |
| `TOMLDecodeError` | Syntax error in `test.toml` (often a line wrapped while pasting) |
| `Unit mdt-bridge-test.service was already loaded` | A failed unit still holds the name: `sudo systemctl reset-failed mdt-bridge-test` |

After a failure, start the installed bridge again first
(`sudo systemctl start ml-source-bridge.service`) -- otherwise no bridge runs.
