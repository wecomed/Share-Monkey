"""Bounded, aggregate-only connection diagnostics written by a background thread.

``processed_ack_ms`` and ``sample_to_ack_ms`` use the sender's monotonic
clock. They measure round trips through remote processing/acknowledgement,
not one-way network latency or the time a physical display updates.
"""
from collections import deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from . import __version__


class TelemetrySession:
    INTERVAL_SECONDS = 2.0
    MAX_SAMPLES = 256
    MAX_METRICS = 64
    MAX_FILE_BYTES = 1024 * 1024
    SUMMARY_RESERVE_BYTES = 64 * 1024
    KEEP_LOGS = 10
    CLOSE_TIMEOUT_SECONDS = .5
    _NAME = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
    _LOG_NAME = re.compile(r'connection-\d{8}-\d{6}-\d+-[0-9a-f]{8}\.jsonl\Z')

    def __init__(self, transport, controller, directory=None):
        if directory is None:
            base = (Path(os.environ.get('LOCALAPPDATA', Path.home()))
                    if sys.platform == 'win32'
                    else Path.home() / 'Library' / 'Application Support')
            directory = base / 'Share Monkey' / 'logs'
        self.directory = Path(directory)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        self.path = self.directory / f'connection-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl'
        self.transport = transport if transport in ('lan', 'bluetooth') else 'unknown'
        self.role = 'controller' if controller else 'receiver'
        self.platform = sys.platform
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._started = self._closed = self._disabled = False
        self._origin = time.monotonic()
        self._names = set()
        self._observations = {}
        self._counts = {}
        self._gauges = {}
        self._counters = {}
        self._bytes_written = 0
        self._limit_reached = False

    def start(self):
        """Start logging once; no directory or file IO runs on the caller."""
        with self._lock:
            if self._started or self._closed:
                return self
            self._started = True
            self._thread = threading.Thread(target=self._run,
                                            name='ShareMonkey-performance', daemon=True)
            worker = self._thread
        try:
            worker.start()
        except Exception:
            # Diagnostics must never stop input sharing, even during teardown.
            with self._lock:
                self._disabled = True
        return self

    def _accept(self, name):
        # Call under _lock. No values or names derived from user input belong
        # here; the identifier check also prevents arbitrary text in log keys.
        if (self._closed or self._disabled or not isinstance(name, str)
                or not self._NAME.fullmatch(name)):
            return False
        if name not in self._names:
            if len(self._names) >= self.MAX_METRICS:
                return False
            self._names.add(name)
        return True

    @staticmethod
    def _number(value):
        if type(value) not in (int, float):
            return None
        try:
            result = float(value)
        except (ValueError, OverflowError):
            return None
        return result if math.isfinite(result) else None

    def observe(self, name, ms):
        """Record a nonnegative duration in milliseconds, without file IO."""
        value = self._number(ms)
        if value is None or value < 0:
            return
        with self._lock:
            if self._accept(name):
                bucket = self._observations.get(name)
                if bucket is None:
                    bucket = self._observations[name] = deque(maxlen=self.MAX_SAMPLES)
                bucket.append(value)
                self._counts[name] = self._counts.get(name, 0) + 1

    def gauge(self, name, value):
        """Keep only the latest finite numeric value of a named gauge."""
        value = self._number(value)
        if value is None:
            return
        with self._lock:
            if self._accept(name):
                self._gauges[name] = value

    def increment(self, name, n=1):
        """Add to a session-wide integer counter; no individual events persist."""
        if type(n) is not int or not 0 <= n <= 2**63 - 1:
            return
        with self._lock:
            if self._accept(name):
                self._counters[name] = self._counters.get(name, 0) + n

    def close(self):
        """Request one final summary, with a bounded wait outside the worker.

        An unavailable/slow disk cannot indefinitely block engine shutdown.
        The worker owns all file IO, including the final flush. Closing an
        unstarted session creates no file, and repeated calls are harmless.
        """
        with self._lock:
            self._closed = True
            worker = self._thread
        self._stop.set()
        if worker is not None and worker is not threading.current_thread():
            try:
                worker.join(self.CLOSE_TIMEOUT_SECONDS)
            except RuntimeError:
                # close() may race the small gap before Thread.start().
                pass

    def _snapshot(self, kind):
        with self._lock:
            samples = {name: (self._counts[name], list(values))
                       for name, values in self._observations.items()}
            gauges, counters = dict(self._gauges), dict(self._counters)
        observations = {}
        for name, (count, values) in samples.items():
            values.sort()
            length = len(values)
            observations[name] = {
                'count': count,
                'sample_count': length,
                'min_ms': values[0],
                'mean_ms': math.fsum(value / length for value in values),
                'p50_ms': values[math.ceil(length * .5) - 1],
                'p95_ms': values[math.ceil(length * .95) - 1],
                'max_ms': values[-1],
            }
        return {
            'kind': kind,
            'timestamp_utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'elapsed_seconds': round(time.monotonic() - self._origin, 6),
            'observations': observations,
            'gauges': gauges,
            'counters': counters,
            'file_limit_reached': self._limit_reached,
        }

    def _write(self, handle, record, final=False):
        raw = json.dumps(record, ensure_ascii=True, allow_nan=False,
                         separators=(',', ':')) + '\n'
        size = len(raw.encode('utf-8'))
        reserve = min(self.SUMMARY_RESERVE_BYTES, self.MAX_FILE_BYTES // 4)
        ceiling = self.MAX_FILE_BYTES if final else self.MAX_FILE_BYTES - reserve
        if self._bytes_written + size > ceiling:
            self._limit_reached = True
            return
        handle.write(raw)
        handle.flush()
        self._bytes_written += size

    def _prune(self):
        try:
            candidates = []
            for path in self.directory.glob('connection-*.jsonl'):
                if self._LOG_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file():
                    candidates.append((path.stat().st_mtime_ns, path.name, path))
            # Never delete this session even if another process creates a log
            # during startup. Ignore unrelated names and symbolic links.
            others = [item for item in sorted(candidates, reverse=True) if item[2] != self.path]
            for _, _, path in others[max(0, self.KEEP_LOGS - 1):]:
                try:
                    path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _run(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open('x', encoding='utf-8') as handle:
                self._prune()
                self._write(handle, {
                    'kind': 'session_start', 'schema_version': 1,
                    'version': __version__, 'role': self.role,
                    'platform': self.platform, 'transport': self.transport,
                    'timestamp_utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                    'sample_limit': self.MAX_SAMPLES,
                    'statistics_window': 'latest_256_observations; counts_are_session_totals',
                    'latency_definition': {
                        'processed_ack_ms': 'sender_monotonic_round_trip_through_remote_processing',
                        'sample_to_ack_ms': 'sender_monotonic_sample_to_processed_ack_round_trip',
                        'excludes': 'not_one_way_network_latency_or_physical_display_latency',
                    },
                })
                while not self._stop.wait(self.INTERVAL_SECONDS):
                    self._write(handle, self._snapshot('interval'))
                self._write(handle, self._snapshot('summary'), final=True)
        except Exception:
            # Permissions, disk-full and disappearing folders only disable
            # diagnostics. Never call back into or raise into the engine.
            with self._lock:
                self._disabled = True
