# ml-debug

Pretty-prints every ML telegram on the bus, decoded into TO / FROM /
TYPE / payload-type / payload fields. User-launched, no service.

```sh
sudo python3 /opt/mdt-tools/ml-debug/ml_debug.py
```

It subscribes to `link:ml:receive` (RX from the bus) and `link:ml:transmit`
(what we ourselves are sending), so you see both sides interleaved. Sudo
is only needed for redis access if your redis is locked down; otherwise
plain user works.

Stop with Ctrl-C.
