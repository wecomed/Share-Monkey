"""Scoped Windows 11 BLE connection priority, on the existing MTA event loop.

The device is borrowed from the pinned Bleak 3.0.2 WinRT backend; we never open
another radio connection or close Bleak's device. Older Windows/driver failures
disable only this optional optimization. No registry/global radio settings change.
"""
import asyncio


class WindowsConnectionPriority:
    IDLE_GRACE_SECONDS = 5.0

    def __init__(self, device, parameters, metrics=None, loop=None):
        self.device, self.parameters, self.metrics = device, parameters, metrics
        self.loop = loop or asyncio.get_running_loop()
        self.request = None
        self.idle = None
        self.token = None
        self.closed = False
        try:
            self.token = device.add_connection_parameters_changed(self._changed)
        except Exception:
            pass  # Older OS/driver: request and sampling can still be available.

    def gauge(self, name, value):
        if self.metrics is not None:
            self.metrics.gauge(name, value)

    def _changed(self, sender, event):
        # WinRT events can arrive on a foreign thread. Keep all device/lease
        # operations on the connector's MTA loop, including teardown races.
        if not self.closed:
            try:
                self.loop.call_soon_threadsafe(self.sample)
            except RuntimeError:
                pass

    def sample(self):
        if self.closed:
            return
        try:
            value = self.device.get_connection_parameters()
            interval = int(value.connection_interval)
            if not 6 <= interval <= 3200:
                raise ValueError('Connection interval is not negotiated yet')
            self.gauge('ble_connection_interval_ms', interval * 1.25)
            self.gauge('ble_peripheral_latency', int(value.connection_latency))
            self.gauge('ble_link_timeout_ms', int(value.link_timeout) * 10)
        except Exception:
            self.gauge('ble_connection_parameters_available', 0)
        else:
            self.gauge('ble_connection_parameters_available', 1)
        if self.request is not None:
            try:
                self.gauge('ble_priority_request_status', int(self.request.status))
            except Exception:
                pass

    def set_active(self, active):
        if self.closed:
            return
        if self.idle is not None:
            self.idle.cancel()
            self.idle = None
        if active and self.request is None:
            try:
                self.request = self.device.request_preferred_connection_parameters(self.parameters)
                if self.request is None:
                    raise RuntimeError('No preferred connection request returned')
                self.gauge('ble_priority_supported', 1)
                self.gauge('ble_priority_held', 1)
            except Exception:
                self.gauge('ble_priority_supported', 0)
                self.gauge('ble_priority_held', 0)
        elif not active and self.request is not None:
            # Keep rapid back-and-forth crossings in the same request lifetime.
            # An idle connection releases its extra radio/battery allocation.
            self.idle = self.loop.call_later(self.IDLE_GRACE_SECONDS, self._release)
        self.sample()

    def _release(self):
        self.idle = None
        request, self.request = self.request, None
        if request is not None:
            try:
                request.close()
            except Exception:
                # Disconnect/reset can invalidate the native request first.
                pass
        self.gauge('ble_priority_held', 0)
        self.sample()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.idle is not None:
            self.idle.cancel()
            self.idle = None
        if self.token is not None:
            try:
                self.device.remove_connection_parameters_changed(self.token)
            except Exception:
                pass
            self.token = None
        self._release()
