"""Bus listener -- music recognition on what the ML bus carries.

The HAT's ADC can hear the MasterLink audio lines (PCM1862 VIN1; VIN2
carries the same). So when the audio master plays a source the Pi doesn't
provide itself -- CD, A.MEM, RADIO, ... -- we can listen along and identify
the music the same way the turntable does ([turntable] recognize):

    arecord (ADC, VIN1) | audio_tap.py --recognize --channel link:ml:recognized

Nothing goes back to the DAC; we only listen. Nothing is written to disk.

WHEN. Follows the state tracker (`link:ml:state`): listens while the audio
master reports Playing a source that is
  * not one of this bridge's own sources (those know what they play), and
  * in [ml_listen] sources, if that list is set (names like "CD" or source
    bytes like "0x8d"; empty = every other source).
A new track number from the audio master (CD) starts a new track for the
recognizer, like a silent gap does.

ADC. Shared with the turntable, which has priority (core/adc.py): when the
turntable starts its loopback, we stop capturing and resume afterwards.
On each start we point the ADC input mux at the ML input (adc_input_reg,
default 0x41 = VIN1) at 0 dB PGA.

RESULT. Published like a source of our own, on state:nowplaying /
link:ml:nowplaying (so ha-notifier passes it on), keyed by the bus
source's byte, with "origin": "ml_listen" in every message:

    {"source": "CD", "source_byte": "0x8d", "provider": "ml_listen",
     "display": "CD", "state": "playing", "title": ..., "artist": ...,
     "album": ..., "art_url": <Shazam cover URL>, "track": "3",
     "origin": "ml_listen"}

`state` is playing while we listen (title etc. empty until recognized),
idle when the source stops or another one takes over.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Optional

import redis

from core import adc, nowplaying
from core.bus import log

STATE_CHAN = "link:ml:state"
STATE_KEY = "state:ml"
LISTEN_CHAN = "link:ml:recognized"
ORIGIN = "ml_listen"

_REG_PGA_L, _REG_PGA_R = 0x01, 0x02
_REG_ADC1L_IN, _REG_ADC1R_IN = 0x06, 0x07
_ADC_IN_ML = 0x41            # VIN1, single-ended: the ML audio lines
_RESTART_BACKOFF_S = 10.0    # after the capture died unexpectedly


def _parse_sources(raw) -> tuple[set[int], set[str]]:
    """[ml_listen] sources -> (source bytes, lower-case names)."""
    if raw in (None, ""):
        return set(), set()
    if not isinstance(raw, list):
        raise SystemExit(f"[ml_listen] sources must be a list, got {raw!r}")
    nums: set[int] = set()
    names: set[str] = set()
    for s in raw:
        if isinstance(s, int):
            nums.add(s & 0xFF)
        elif isinstance(s, str) and s.strip().lower().startswith("0x"):
            nums.add(int(s, 16) & 0xFF)
        elif isinstance(s, str) and s.strip():
            names.add(s.strip().lower())
        else:
            raise SystemExit(f"[ml_listen] sources: bad entry {s!r}")
    return nums, names


class BusListener:
    def __init__(self, *, cfg: dict, own_sources: set[int],
                 redis_host: str = "localhost", redis_port: int = 6379) -> None:
        self._own = set(own_sources)
        self._src_nums, self._src_names = _parse_sources(cfg.get("sources"))
        self._cap_dev = str(cfg.get("capture_device", "hw:sndrpihifiberry,0"))
        self._rate = int(cfg.get("sample_rate", 48000))
        self._channels = int(cfg.get("channels", 2))
        self._format = str(cfg.get("format", "S32_LE"))
        self._py = str(cfg.get("recognize_python",
                               "/opt/mdt-tools/.recognize-venv/bin/python"))
        self._snippet_s = float(cfg.get("recognize_snippet_s", 10.0))
        self._silence_db = float(cfg.get("recognize_silence_db", -50.0))
        self._adc_setup = bool(cfg.get("adc_setup", True))
        self._adc_input = int(cfg.get("adc_input_reg", _ADC_IN_ML))
        self._pga_db = float(cfg.get("pga_db", 0.0))
        self._i2c_bus = int(cfg.get("i2c_bus", 1))
        self._i2c_addr = str(cfg.get("i2c_addr", "0x4a"))
        self._i2cset = str(cfg.get("i2cset_path") or shutil.which("i2cset")
                           or "/usr/sbin/i2cset")
        self._redis_host, self._redis_port = redis_host, redis_port
        self._r = redis.StrictRedis(host=redis_host, port=redis_port, db=0,
                                    socket_keepalive=True)

        self._lock = threading.Lock()            # capture state below
        self._rec: Optional[subprocess.Popen] = None
        self._tap: Optional[subprocess.Popen] = None
        self._src: Optional[int] = None          # source we listen to
        self._src_name = ""
        self._track: Optional[int] = None
        self._recognized: Optional[dict] = None
        self._suspended = False
        self._retry_at = 0.0

        self._am: dict = {}                      # latest state:ml "am" part
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not os.access(self._py, os.X_OK):
            log(f"[listen] {self._py} is missing -- re-run install.sh "
                f"(it sets up the recognition venv when [ml_listen] is "
                f"enabled); bus listener off", err=True)
            return
        self._thread = threading.Thread(target=self._run, name="ml-listen",
                                        daemon=True)
        self._thread.start()
        what = (", ".join(sorted([f"0x{n:02x}" for n in self._src_nums]
                                 + sorted(self._src_names)))
                or "every source not our own")
        log(f"[listen] bus listener on: {what}; ADC input "
            f"0x{self._adc_input:02x}")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._stop_capture(publish_idle=True)

    # ---- ADC arbitration (core/adc.py) ---------------------------------------

    def suspend(self) -> None:
        with self._lock:
            self._suspended = True
            if self._rec is not None:
                log("[listen] turntable takes the ADC -- pausing")
            self._stop_capture(publish_idle=True)

    def resume(self) -> None:
        with self._lock:
            self._suspended = False
        self._wake.set()

    # ---- decisions -----------------------------------------------------------

    def wanted(self, am: dict) -> Optional[tuple[int, str]]:
        """(source byte, name) to listen to for this audio-master state, or
        None."""
        if not am.get("playing"):
            return None
        try:
            src = int(str(am.get("source")), 16)
        except (TypeError, ValueError):
            return None
        if src in self._own:
            return None
        name = str(am.get("source_name") or f"0x{src:02x}")
        if (self._src_nums or self._src_names) and not (
                src in self._src_nums or name.lower() in self._src_names):
            return None
        return src, name

    @staticmethod
    def _track_of(am: dict) -> Optional[int]:
        t = am.get("track")
        return t if isinstance(t, int) and 0 < t < 255 else None

    def _evaluate(self) -> None:
        am = self._am
        want = self.wanted(am)
        with self._lock:
            if self._suspended or adc.turntable_active():
                want = None
            if self._rec is not None and (self._rec.poll() is not None
                                          or self._tap.poll() is not None):
                log("[listen] capture ended unexpectedly -- retrying in "
                    f"{_RESTART_BACKOFF_S:.0f} s", err=True)
                self._stop_capture(publish_idle=False)
                self._retry_at = time.monotonic() + _RESTART_BACKOFF_S
            if want is None:
                self._stop_capture(publish_idle=True)
                return
            src, name = want
            if self._rec is not None and self._src != src:
                self._stop_capture(publish_idle=True)
            if self._rec is None:
                if time.monotonic() < self._retry_at:
                    return
                self._start_capture(src, name, self._track_of(am))
                return
            track = self._track_of(am)
            if track is not None and track != self._track:
                if self._track is not None:
                    log(f"[listen] {name}: track {self._track} -> {track}")
                    self._signal_new_track()
                    self._recognized = None
                self._track = track
                self._publish()

    # ---- capture -------------------------------------------------------------

    def _i2c(self, reg: int, val: int) -> None:
        cmd = [self._i2cset, "-f", "-y", str(self._i2c_bus), self._i2c_addr,
               f"0x{reg:02x}", f"0x{val:02x}", "b"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=5)
        except (OSError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired) as e:
            log(f"[listen] i2cset {cmd[-3]}={cmd[-2]} failed: {e}", err=True)

    def _start_capture(self, src: int, name: str, track: Optional[int]) -> None:
        """With self._lock held."""
        if self._adc_setup:
            pga = int(round(max(-12.0, min(40.0, self._pga_db)) * 2)) & 0xFF
            self._i2c(_REG_ADC1L_IN, self._adc_input)
            self._i2c(_REG_ADC1R_IN, self._adc_input)
            self._i2c(_REG_PGA_L, pga)
            self._i2c(_REG_PGA_R, pga)
        bits = 16 if "16" in self._format else 32
        tap_py = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "providers", "audio_tap.py")
        try:
            # Two processes instead of a shell pipeline: SIGUSR1 ("new
            # track") must reach the tap only -- arecord would die of it.
            self._rec = subprocess.Popen(
                ["arecord", "-q", "-D", self._cap_dev, "-f", self._format,
                 "-r", str(self._rate), "-c", str(self._channels)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, start_new_session=True)
            self._tap = subprocess.Popen(
                [self._py, "-u", tap_py, "--rate", str(self._rate),
                 "--channels", str(self._channels), "--bits", str(bits),
                 "--recognize", "--recognize-s", str(self._snippet_s),
                 "--silence-db", str(self._silence_db),
                 "--channel", LISTEN_CHAN,
                 "--redis-host", str(self._redis_host),
                 "--redis-port", str(int(self._redis_port))],
                stdin=self._rec.stdout, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, start_new_session=True)
            self._rec.stdout.close()            # the tap owns the read end now
        except OSError as e:
            log(f"[listen] can't start the capture: {e}", err=True)
            self._kill(self._rec)
            self._rec = self._tap = None
            self._retry_at = time.monotonic() + _RESTART_BACKOFF_S
            return
        for proc, tag in ((self._rec, "arecord"), (self._tap, "tap")):
            threading.Thread(target=self._pump_stderr, args=(proc, tag),
                             name=f"ml-listen-{tag}", daemon=True).start()
        self._src, self._src_name, self._track = src, name, track
        self._recognized = None
        log(f"[listen] listening to {name} (0x{src:02x})")
        self._publish()

    def _stop_capture(self, *, publish_idle: bool) -> None:
        """With self._lock held. Returns once the ADC is free."""
        rec, tap = self._rec, self._tap
        self._rec = self._tap = None
        if rec is None:
            return
        self._kill(tap)
        self._kill(rec)
        src, name = self._src, self._src_name
        self._recognized = None
        self._src, self._track = None, None
        log(f"[listen] stopped listening to {name}")
        if publish_idle and src is not None:
            self._write(src, self._blob(src, name, state="idle"))

    @staticmethod
    def _kill(proc: Optional[subprocess.Popen]) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=2.0)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
            pass

    def _signal_new_track(self) -> None:
        """With self._lock held."""
        if self._tap is not None and self._tap.poll() is None:
            try:
                os.kill(self._tap.pid, signal.SIGUSR1)
            except ProcessLookupError:
                pass

    @staticmethod
    def _pump_stderr(proc: subprocess.Popen, tag: str) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            if line.startswith("[tap]"):
                log(f"[listen] {line}")
            else:
                log(f"[listen] {tag}: {line}", err=True)

    # ---- results -------------------------------------------------------------

    def _blob(self, src: int, name: str, *, state: str) -> dict[str, str]:
        rec = self._recognized if state == "playing" else None
        return {
            "source": name, "source_byte": f"0x{src:02x}",
            "provider": ORIGIN if state == "playing" else "",
            "display": name if state == "playing" else "",
            "state": state,
            "title": (rec or {}).get("title", ""),
            "artist": (rec or {}).get("artist", ""),
            "album": (rec or {}).get("album", ""),
            "art_url": (rec or {}).get("cover_url", ""),
            "track": str(self._track) if state == "playing" and self._track else "",
            "origin": ORIGIN,
        }

    def _publish(self) -> None:
        """With self._lock held: the current view of the source we listen to."""
        if self._src is not None:
            self._write(self._src, self._blob(self._src, self._src_name,
                                              state="playing"))

    def _write(self, src: int, blob: dict[str, str]) -> None:
        try:
            nowplaying.write(self._r, src, blob)
        except redis.exceptions.RedisError as e:
            log(f"[listen] redis publish failed: {e}", err=True)

    def _on_recognized(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        with self._lock:
            if self._rec is None:
                return                          # stale: we stopped meanwhile
            if msg.get("state") == "confirmed":
                self._recognized = {k: str(msg.get(k) or "") for k in
                                    ("title", "artist", "album", "cover_url")}
            elif self._recognized is None:
                return                          # "cleared" with nothing shown
            else:
                self._recognized = None
            self._publish()

    # ---- thread ----------------------------------------------------------------

    def _run(self) -> None:
        ps = None
        while not self._stop.is_set():
            try:
                if ps is None:
                    ps = self._r.pubsub(ignore_subscribe_messages=True)
                    ps.subscribe(STATE_CHAN, LISTEN_CHAN)
                    raw = self._r.get(STATE_KEY)
                    if raw:
                        self._am = (json.loads(raw) or {}).get("am") or {}
                    self._evaluate()
                m = ps.get_message(timeout=1.0)
                if m is not None:
                    chan = m.get("channel")
                    chan = chan.decode() if isinstance(chan, bytes) else chan
                    data = m.get("data")
                    data = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
                    if chan == LISTEN_CHAN:
                        self._on_recognized(data)
                        continue
                    try:
                        self._am = (json.loads(data) or {}).get("am") or {}
                    except ValueError:
                        continue
                self._wake.clear()
                self._evaluate()                # also once a second: retries, resume
            except redis.exceptions.RedisError as e:
                log(f"[listen] redis error: {e}", err=True)
                if ps is not None:
                    try: ps.close()
                    except Exception: pass
                    ps = None
                self._stop.wait(2.0)
            except Exception as e:              # never take the bridge down
                log(f"[listen] {type(e).__name__}: {e}", err=True)
                self._stop.wait(2.0)
        if ps is not None:
            try: ps.close()
            except Exception: pass


def validate(cfg: dict) -> None:
    """Config check for --check-config: raises SystemExit on bad values."""
    _parse_sources(cfg.get("sources"))
    for key in ("adc_input_reg", "sample_rate", "channels", "i2c_bus"):
        if key in cfg:
            int(cfg[key])
    for key in ("recognize_snippet_s", "recognize_silence_db", "pga_db"):
        if key in cfg:
            float(cfg[key])
