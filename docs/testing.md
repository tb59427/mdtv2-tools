# Testing changes on the Pi without touching the installed version

`install.sh` overwrites `/opt/mdt-tools/` and the systemd units. To try a
work-in-progress checkout while keeping the installed version as a fallback,
run it as a **test instance** next to it: stop the installed bridge, start
the checkout's bridge (and ha-notifier) as transient systemd units, and go
back with two commands -- or a reboot.

Broker and state-tracker keep running; only the bridge and ha-notifier are
swapped. Examples use `pi@masterlink-bridge` and a checkout on a Mac.

## One-time setup (on the Pi)

```sh
sudo install -d -o "$USER" -g "$USER" -m 755 /opt/mdt-tools-test
sudo cp /etc/ml-source-bridge.toml /opt/mdt-tools-test/test.toml
sudo chown "$USER":mdt /opt/mdt-tools-test/test.toml
sudo chmod 640 /opt/mdt-tools-test/test.toml
nano /opt/mdt-tools-test/test.toml      # e.g. add [ha_notifier]
```

The directory belongs to your login user, so copying needs no sudo; the
services run as `mdt`, which reads it through the world/group bits. Check the
config parses:

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
sudo systemctl stop ml-source-bridge.service

sudo systemd-run --unit=mdt-bridge-test \
  -p User=mdt -p Group=audio -p SupplementaryGroups=mdt \
  /usr/bin/python3 -u /opt/mdt-tools-test/repo/ml-source-bridge/ml_source_bridge.py \
  --config /opt/mdt-tools-test/test.toml --log-file ""

sudo systemd-run --unit=mdt-notifier-test -p User=mdt \
  /usr/bin/python3 -u /opt/mdt-tools-test/repo/ha-notifier/ha_notifier.py \
  --config /opt/mdt-tools-test/test.toml

sleep 5; systemctl is-active ml-source-bridge mdt-bridge-test mdt-notifier-test
# expected: inactive active active
```

- `Group=audio` (as in the real unit) replaces `mdt`'s own group;
  `SupplementaryGroups=mdt` keeps it so the bridge can read `test.toml`.
- `--log-file ""` keeps the test bridge out of `/tmp/mdt.log`, which the
  installed services share.
- **Never run both bridges at once** -- they claim the same ML address.

Logs and state:

```sh
sudo journalctl -u mdt-bridge-test -u mdt-notifier-test -f
redis-cli SUBSCRIBE link:ml:nowplaying
```

After copying new code: `sudo systemctl restart mdt-bridge-test mdt-notifier-test`.

## Back to the installed version

```sh
sudo systemctl stop mdt-bridge-test mdt-notifier-test
sudo systemctl start ml-source-bridge.service
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
