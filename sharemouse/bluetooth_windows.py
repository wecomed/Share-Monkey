"""Windows BLE central on dedicated asyncio/MTA threads.

Neither importing this module nor constructing a browser requests Bluetooth
access. The GUI consumes callbacks through Session's existing event queues.
The Mac advertises the GATT service regardless of which computer owns the mouse.
"""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
import json
import socket
import sys
import threading
import time

from .bluetooth_transport import (BleStream, BluetoothPeer, INFO_UUID, RX_UUID,
                                  SERVICE_UUID, TRANSPORT_PROTOCOL, TX_UUID,
                                  normalize_error)

PEER_TTL = 12.0
DEVICE_TTL = 90.0
MAX_PEERS = 128
_devices = {}
_devices_lock = threading.Lock()


def _bleak():
    # WinRT must be imported on our MTA worker, never on the GUI STA thread.
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise RuntimeError('当前安装缺少蓝牙组件，请重新安装最新版 Share Monkey') from exc
    return BleakClient, BleakScanner


def _preferred_parameters():
    from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters
    return BluetoothLEPreferredConnectionParameters.throughput_optimized


@contextmanager
def _mta_thread():
    ole32 = None
    if sys.platform == 'win32':
        import ctypes
        ole32 = ctypes.OleDLL('ole32')
        ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        ole32.CoInitializeEx.restype = ctypes.c_int32
        # A new worker has no GUI COM apartment; COINIT_MULTITHREADED = 0.
        result = ole32.CoInitializeEx(None, 0)
        if result not in (0, 1):
            raise OSError(f'无法初始化蓝牙工作线程 (HRESULT {result:#x})')
    try:
        yield
    finally:
        if ole32 is not None:
            ole32.CoUninitialize()


def _remember(device, now):
    key = device.address.casefold()
    with _devices_lock:
        for address, (_, seen) in list(_devices.items()):
            if now - seen > DEVICE_TTL:
                _devices.pop(address, None)
        if key not in _devices and len(_devices) >= MAX_PEERS:
            _devices.pop(min(_devices, key=lambda address: _devices[address][1]))
        _devices[key] = (device, now)


def _known_device(address):
    with _devices_lock:
        entry = _devices.get(address.casefold())
        return entry[0] if entry and time.monotonic() - entry[1] <= DEVICE_TTL else None


class WindowsBleBrowser:
    def __init__(self, on_peers, on_error=lambda message: None):
        self.on_peers = on_peers
        self.on_error = on_error
        self.done = threading.Event()
        self.thread = None
        self.loop = None
        self.task = None
        self._peers = {}

    def start(self):
        if self.thread is not None:
            return self
        self.thread = threading.Thread(target=self._run, name='ShareMonkey-BLE-scan', daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.done.set()
        loop, task = self.loop, self.task
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def _run(self):
        try:
            with _mta_thread():
                asyncio.run(self._scan())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self.done.is_set():
                self.on_error(normalize_error(exc))
        finally:
            self.done.set()

    def _advertised(self, device, advertisement):
        if self.done.is_set():
            return
        services = getattr(advertisement, 'service_uuids', None) or ()
        if SERVICE_UUID not in {str(value).casefold() for value in services}:
            return
        address = getattr(device, 'address', '')
        if not isinstance(address, str) or not 1 <= len(address) <= 128:
            return
        name = getattr(advertisement, 'local_name', None) or getattr(device, 'name', None) or 'Share Monkey Mac'
        name = ''.join(char for char in str(name) if ord(char) >= 32)[:80] or 'Share Monkey Mac'
        peer = BluetoothPeer(address, name)
        key, now = address.casefold(), time.monotonic()
        if key not in self._peers and len(self._peers) >= MAX_PEERS:
            return
        changed = key not in self._peers or self._peers[key][0] != peer
        self._peers[key] = (peer, now)
        _remember(device, now)
        if changed:
            self.on_peers([item[0] for item in self._peers.values()])

    def _expire(self, now):
        expired = [key for key, (_, seen) in self._peers.items() if now - seen > PEER_TTL]
        for key in expired:
            self._peers.pop(key, None)
        if expired:
            self.on_peers([item[0] for item in self._peers.values()])

    async def _scan(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        if self.done.is_set():
            return
        _, scanner_type = _bleak()
        async with scanner_type(detection_callback=self._advertised, service_uuids=[SERVICE_UUID]):
            while not self.done.is_set():
                self._expire(time.monotonic())
                await asyncio.sleep(.2)


class WindowsBleConnector:
    """One connection lifetime. Reconnect by creating a fresh connector."""

    def __init__(self, address, status=lambda message: None, metrics=None):
        self.address = address
        self.status = status
        self.done = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.loop = None
        self.stream = None
        self.info = None
        self.error = None
        self._client = None
        self._closing = None
        self._task = None
        self._rx = None
        self._write_size = 20
        self._write_lock = None
        self._disconnecting = False
        self.metrics = metrics
        self._priority = None

    def _start_priority(self):
        # Bridge only the pinned Bleak 3.0.2 WinRT backend's existing device.
        # No new BluetoothLEDevice, pairing or access request is created.
        requester = getattr(getattr(self._client, '_backend', None), '_requester', None)
        if requester is None:
            return
        try:
            from .bluetooth_priority import WindowsConnectionPriority
            parameters = _preferred_parameters()
            self._priority = WindowsConnectionPriority(requester, parameters, self.metrics)
            self._priority.set_active(True)
            self._priority.set_active(False)  # bounded warm-up for the encrypted handshake
        except Exception:
            if self.metrics is not None:
                self.metrics.gauge('ble_priority_supported', 0)

    def set_input_active(self, active):
        loop = self.loop
        if self.done.is_set() or loop is None or loop.is_closed():
            return
        def update():
            if self._priority is not None and not self.done.is_set():
                self._priority.set_active(bool(active))
        try:
            loop.call_soon_threadsafe(update)
        except RuntimeError:
            pass

    def connect(self, stop_event, timeout=15.0):
        if self.thread is not None:
            raise RuntimeError('此蓝牙连接已使用，请重新连接')
        if self.done.is_set() or stop_event.is_set():
            raise ConnectionAbortedError('蓝牙连接已取消')
        if not isinstance(self.address, str) or not 1 <= len(self.address) <= 128:
            raise ValueError('请选择搜索到的 Mac 蓝牙设备')
        self.thread = threading.Thread(target=self._run, args=(timeout,),
                                       name='ShareMonkey-BLE-connect', daemon=True)
        self.thread.start()
        deadline = time.monotonic() + timeout + 1.0
        while not self.ready.wait(.05):
            if stop_event.is_set() or self.done.is_set():
                self.close()
                raise ConnectionAbortedError('蓝牙连接已取消')
            if time.monotonic() >= deadline:
                self.close()
                raise socket.timeout('连接蓝牙超时，请确认 Mac 已开启蓝牙等待')
        if stop_event.is_set():
            self.close()
            raise ConnectionAbortedError('蓝牙连接已取消')
        if self.error is not None:
            raise ConnectionError(normalize_error(self.error)) from self.error
        if self.stream is None or self.stream.closed:
            raise ConnectionError('Mac 蓝牙连接已断开，请重新连接')
        return self.stream

    def close(self):
        # Called by Stream.finish, including inside notification callbacks.
        # Never wait for this thread from itself or from the GUI.
        self.done.set()
        loop, closing = self.loop, self._closing
        if loop is not None and closing is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(closing.set)
            except RuntimeError:
                pass
        # Stop discovery/connect immediately, without cancelling cleanup.
        task = self._task
        if (not self.ready.is_set() and not self._disconnecting and loop is not None
                and task is not None and not loop.is_closed()):
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    stop = close

    def _run(self, timeout):
        try:
            with _mta_thread():
                asyncio.run(self._session(timeout))
        except asyncio.CancelledError:
            if self.error is None:
                self.error = ConnectionAbortedError('蓝牙连接已取消')
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()
            if self.stream is not None:
                self.stream.finish(self.error)
            self.ready.set()

    def _disconnected(self, client):
        if self._disconnecting:
            return
        if self.stream is not None:
            self.stream.finish(ConnectionResetError('蓝牙连接已断开，本机键鼠已恢复'))
        else:
            self.error = ConnectionResetError('Mac 在连接过程中断开了蓝牙')
            self.close()

    async def _session(self, timeout):
        self.loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._closing = asyncio.Event()
        self._write_lock = asyncio.Lock()
        if self.done.is_set():
            raise ConnectionAbortedError('蓝牙连接已取消')
        try:
            await asyncio.wait_for(self._open(timeout), timeout)
            if self.done.is_set() or not self._client.is_connected:
                raise ConnectionAbortedError('蓝牙连接已断开')
            self.ready.set()
            await self._closing.wait()
        finally:
            self._disconnecting = True
            if self._priority is not None:
                self._priority.close()
                self._priority = None
            if self._client is not None:
                try:
                    await asyncio.wait_for(self._client.disconnect(), 4.0)
                except Exception:
                    pass

    async def _open(self, timeout):
        client_type, scanner_type = _bleak()
        device = _known_device(self.address)
        if device is None:
            self.status('正在搜索 Mac 蓝牙设备…')
            device = await scanner_type.find_device_by_address(
                self.address, timeout=timeout, service_uuids=[SERVICE_UUID])
        if device is None:
            raise ConnectionError('找不到 Mac，请在 Mac 中选择蓝牙并点击“开启蓝牙等待”')
        if self.done.is_set():
            raise ConnectionAbortedError('蓝牙连接已取消')
        self.status('正在连接 Mac 蓝牙…')
        self._client = client_type(device, disconnected_callback=self._disconnected,
                                   services=[SERVICE_UUID], timeout=timeout,
                                   winrt={'use_cached_services': False})
        await self._client.connect()
        self._start_priority()
        raw_info = bytes(await self._client.read_gatt_char(INFO_UUID))
        if len(raw_info) > 4096:
            raise ConnectionError('Mac 蓝牙服务信息过大')
        try:
            info = json.loads(raw_info)
        except (ValueError, UnicodeError) as exc:
            raise ConnectionError('Mac 蓝牙服务信息无效，请升级两端客户端') from exc
        if (not isinstance(info, dict) or type(info.get('transport_protocol')) is not int
                or info['transport_protocol'] != TRANSPORT_PROTOCOL):
            raise ConnectionError('两端蓝牙协议不兼容，请将 Mac 和 Windows 都更新到 v0.5.2 或更新版本')
        self.info = info
        self._rx = self._client.services.get_characteristic(RX_UUID)
        tx = self._client.services.get_characteristic(TX_UUID)
        if (self._rx is None or 'write' not in self._rx.properties
                or tx is None or 'notify' not in tx.properties):
            raise ConnectionError('Mac 蓝牙服务缺少读写通道，请重启 Mac 客户端后重试')
        self._write_size = max(1, min(512, int(self._client.mtu_size) - 3))
        self.stream = BleStream(writer=self._write, on_close=self.close)
        await self._client.start_notify(tx, self._received)

    def _received(self, characteristic, data):
        if self.stream is not None:
            self.stream.feed(data)

    def _write(self, data, timeout):
        if self.done.is_set() or self.loop is None or self.loop.is_closed():
            raise ConnectionError('蓝牙发送通道已关闭')
        if self.thread is threading.current_thread():
            raise RuntimeError('蓝牙发送不能阻塞蓝牙事件线程')
        future = asyncio.run_coroutine_threadsafe(self._write_packets(bytes(data)), self.loop)
        try:
            # None is supported by Stream.settimeout, but never permit an
            # unbounded native operation after an adapter disappears.
            future.result(timeout=15.0 if timeout is None else timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise socket.timeout('蓝牙发送超时，请检查蓝牙状态和两台电脑的距离') from exc

    async def _write_packets(self, data):
        async with self._write_lock:
            offset = 0
            while offset < len(data):
                if self.done.is_set() or not self._client.is_connected:
                    raise ConnectionResetError('蓝牙连接已断开')
                # WinRT negotiates the PDU asynchronously and may change it
                # after connect/start_notify. Do not freeze an initial MTU23
                # into 20-byte writes for the entire session. Refresh between
                # accepted writes so a later decrease is safe as well.
                self._write_size = max(1, min(512, int(self._client.mtu_size) - 3))
                if self.metrics is not None:
                    self.metrics.gauge('ble_write_value_bytes', self._write_size)
                chunk = data[offset:offset + self._write_size]
                # Mac RX exposes Write only. A response provides backpressure
                # and ensures encrypted stream bytes are never dropped/reordered.
                await self._client.write_gatt_char(
                    self._rx, chunk, response=True)
                offset += len(chunk)
