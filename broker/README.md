# mdtv2-broker

MDT MCU ↔ Redis bridge. Translates between the MCU's framed wire
protocol on `/dev/serial0` (115200 baud) and Redis pub/sub channels:

| Redis channel | Direction | Payload |
|---|---|---|
| `link:ml:transmit` | host → MCU | hex telegram, e.g. `c0c1010a0047002005020001ffff93` (broker computes ML checksum + EOL) |
| `link:ml:receive` | MCU → host | hex telegram with ML checksum + EOL bytes |
| `link:dl86:transmit` / `:receive` | both | DataLink '86 |
| `link:dl80:transmit` / `:receive` | both | DataLink '80 |
| `link:gpio:transmit` / `:receive` | both | board GPIO state byte (PWR_EN, PWR_DET, SL_MA, DAC_PLAY) |

Empty publish to `link:gpio:transmit` triggers a state read.

Logs to `/tmp/mdt.log` and stdout. Run via systemd:

```sh
sudo systemctl enable --now mdtv2-broker.service
sudo journalctl -u mdtv2-broker.service -f
```
