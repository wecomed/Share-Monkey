"""Explicitly started macOS BLE GATT server; importing this module is inert.

Core Bluetooth delivers callbacks on the application's main dispatch queue. The
existing AppKit/Tk event loop services that queue; protocol and encryption work
remain on Engine's worker threads. Never wait for a worker from a native callback.
"""
from collections import deque
from dataclasses import dataclass
import json
import socket
import sys
import threading
import time


_NATIVE = None
_NATIVE_LOCK = threading.Lock()


def _load_native():
    """Load framework/classes only after the user explicitly starts Bluetooth."""
    global _NATIVE
    with _NATIVE_LOCK:
        if _NATIVE is None:
            _NATIVE = _create_native()
        return _NATIVE


def _create_native():
    if sys.platform != 'darwin':
        raise OSError('Mac 蓝牙等待功能只能在 macOS 上运行')
    try:
        import CoreBluetooth as CB
        import Foundation as F
        import objc
    except ImportError as exc:
        raise OSError('此版本缺少 Mac 蓝牙组件，请安装完整的 Share Monkey 客户端') from exc

    class ShareMonkeyPeripheralDelegate(F.NSObject,
                                       protocols=[objc.protocolNamed('CBPeripheralManagerDelegate')]):
        @objc.python_method
        def deliver(self, method, *args):
            owner = getattr(self, 'owner', None)
            if owner is not None:
                try:
                    getattr(owner, method)(*args)
                except Exception as exc:
                    owner._fail(OSError('Mac 蓝牙通信异常：' + str(exc)))

        def peripheralManagerDidUpdateState_(self, manager):
            self.deliver('_state_changed', manager)

        def peripheralManager_didAddService_error_(self, manager, service, error):
            self.deliver('_service_added', manager, service, error)

        def peripheralManagerDidStartAdvertising_error_(self, manager, error):
            self.deliver('_advertising_started', manager, error)

        def peripheralManager_central_didSubscribeToCharacteristic_(self, manager, central, characteristic):
            self.deliver('_subscribed', manager, central, characteristic)

        def peripheralManager_central_didUnsubscribeFromCharacteristic_(self, manager, central, characteristic):
            self.deliver('_unsubscribed', manager, central, characteristic)

        def peripheralManagerIsReadyToUpdateSubscribers_(self, manager):
            self.deliver('_drain')

        def peripheralManager_didReceiveWriteRequests_(self, manager, requests):
            self.deliver('_write_requests', manager, requests)

        def peripheralManager_didReceiveReadRequest_(self, manager, request):
            self.deliver('_read_request', manager, request)

    return CB, F, ShareMonkeyPeripheralDelegate


@dataclass
class _Batch:
    data: bytes
    offset: int = 0
    done: bool = False


class MacBleServer:
    """Publish one encrypted stream to a Windows BLE central.

    The Mac is always the GATT peripheral, independently of keyboard ownership.
    A new server instance represents a new connection attempt. ``stop`` never
    waits for the main loop, so closing a stream from a delegate cannot deadlock.
    """
    BUFFER_LIMIT = 262144

    def __init__(self, status=lambda text: None, role='controller', device_id=None):
        from .bluetooth_transport import TRANSPORT_PROTOCOL
        if role not in ('controller', 'receiver'):
            raise ValueError('Bluetooth role must be controller or receiver')
        self.status = status
        self.role = role
        self.device_id = device_id
        self._condition = threading.Condition(threading.RLock())
        self._cleanup_done = threading.Event()
        self._write_lock = threading.Lock()
        self._native = None
        self._started = False
        self._closed = False
        self._error = None
        self._manager = None
        self._delegate = None
        self._service = None
        self._rx = None
        self._tx = None
        self._info = None
        self._central = None
        self._central_id = None
        self._stream = None
        self._accepted = False
        self._mtu = 20
        self._pending = deque()
        self._queued_bytes = 0
        self._adding_service = False
        self._info_bytes = json.dumps({
            'name': socket.gethostname(), 'platform': 'Mac', 'role': role,
            'transport_protocol': TRANSPORT_PROTOCOL,
        }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    def _report(self, text):
        try:
            self.status(text)
        except Exception:
            # A UI observer must not escape through an Objective-C callback.
            pass

    def _dispatch(self, callback):
        import objc
        _, F, _ = self._native

        def invoke():
            try:
                with objc.autorelease_pool():
                    callback()
            except Exception as exc:
                # Escaping a Python block scheduled by NSOperationQueue can
                # terminate the GUI process; always end the stream instead.
                self._fail(OSError('Mac 蓝牙操作失败：' + str(exc)))

        if threading.current_thread() is threading.main_thread():
            invoke()
        else:
            with objc.autorelease_pool():
                F.NSOperationQueue.mainQueue().addOperationWithBlock_(invoke)

    def start(self):
        with self._condition:
            if self._error is not None:
                raise self._error
            if self._closed:
                raise OSError('蓝牙等待已关闭，请重新连接')
            if self._started:
                return
            self._native = _load_native()
            self._started = True
        self._report('正在启动 Mac 蓝牙，请允许 Share Monkey 使用蓝牙')
        self._dispatch(self._start_native)

    def _start_native(self):
        if self._closed:
            return
        CB, F, Delegate = self._native
        try:
            declaration = F.NSBundle.mainBundle().objectForInfoDictionaryKey_(
                'NSBluetoothAlwaysUsageDescription')
            if not declaration or not str(declaration).strip():
                self._fail(OSError('当前启动程序缺少 macOS 蓝牙隐私声明。请从新版 Share Monkey.app 开启蓝牙；源码蓝牙模式也必须在包含 NSBluetoothAlwaysUsageDescription 的应用容器内运行。'))
                return
            delegate = Delegate.alloc().init()
            delegate.owner = self
            self._delegate = delegate  # Core Bluetooth holds its delegate weakly.
            self._manager = CB.CBPeripheralManager.alloc().initWithDelegate_queue_options_(
                delegate, None, {CB.CBPeripheralManagerOptionShowPowerAlertKey: False})
            if self._closed:
                # Also covers a cancellation/reentrant native callback while
                # initWithDelegate was running, before _manager was assigned.
                self._stop_native()
                return
            if self._manager is None:
                self._fail(OSError('无法创建 Mac 蓝牙服务'))
        except Exception as exc:
            self._fail(OSError('无法启动 Mac 蓝牙：' + str(exc)))

    def accept(self, stop_event, timeout=None):
        if not self._started:
            self.start()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._condition:
                if self._error is not None:
                    raise self._error
                if self._closed or stop_event.is_set():
                    break
                if self._stream is not None:
                    if self._accepted:
                        raise OSError('一个 Mac 蓝牙服务只能接受一个连接')
                    self._accepted = True
                    return self._stream
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._condition.wait(min(0.1, remaining) if remaining is not None else 0.1)
        self.stop()
        if stop_event.is_set():
            raise OSError('蓝牙连接已取消')
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('等待 Windows 蓝牙连接超时，请在 Windows 端扫描并选择这台 Mac')
        raise OSError('蓝牙等待已关闭')

    def _state_changed(self, manager):
        if self._closed:
            return
        CB, _, _ = self._native
        state = int(manager.state())
        if state == CB.CBManagerStatePoweredOn:
            if not self._adding_service:
                self._adding_service = True
                self._publish(manager)
        elif state == CB.CBManagerStateUnauthorized:
            self._fail(PermissionError('Mac 蓝牙权限未开启，请前往系统设置 → 隐私与安全性 → 蓝牙，允许 Share Monkey'))
        elif state == CB.CBManagerStateUnsupported:
            self._fail(OSError('这台 Mac 不支持蓝牙低功耗外设模式，请使用局域网连接'))
        elif state == CB.CBManagerStatePoweredOff:
            self._fail(OSError('Mac 蓝牙已关闭，请在系统设置中打开蓝牙后重新连接'))
        elif self._stream is not None or self._adding_service:
            self._fail(ConnectionError('Mac 蓝牙状态发生变化，连接已断开，请重新连接'))
        else:
            self._report('正在等待 Mac 蓝牙就绪…')

    def _publish(self, manager):
        from .bluetooth_transport import SERVICE_UUID, RX_UUID, TX_UUID, INFO_UUID
        CB, F, _ = self._native
        uuid = CB.CBUUID.UUIDWithString_
        characteristic = CB.CBMutableCharacteristic.alloc
        self._rx = characteristic().initWithType_properties_value_permissions_(
            uuid(RX_UUID), CB.CBCharacteristicPropertyWrite, None, CB.CBAttributePermissionsWriteable)
        self._tx = characteristic().initWithType_properties_value_permissions_(
            uuid(TX_UUID), CB.CBCharacteristicPropertyNotify, None, CB.CBAttributePermissionsReadable)
        self._info = characteristic().initWithType_properties_value_permissions_(
            uuid(INFO_UUID), CB.CBCharacteristicPropertyRead,
            F.NSData.dataWithBytes_length_(self._info_bytes, len(self._info_bytes)),
            CB.CBAttributePermissionsReadable)
        self._service = CB.CBMutableService.alloc().initWithType_primary_(uuid(SERVICE_UUID), True)
        self._service.setCharacteristics_([self._rx, self._tx, self._info])
        manager.addService_(self._service)

    def _service_added(self, manager, service, error):
        if self._closed:
            return
        if error is not None:
            self._fail(OSError('无法发布 Mac 蓝牙服务：' + str(error)))
            return
        if service != self._service:
            return
        from .bluetooth_transport import SERVICE_UUID
        CB, _, _ = self._native
        manager.startAdvertising_({
            CB.CBAdvertisementDataServiceUUIDsKey: [CB.CBUUID.UUIDWithString_(SERVICE_UUID)],
            # Leave the 128-bit service UUID in the ordinary advertisement;
            # Windows cannot discover Apple's overflow-only service UUIDs.
            CB.CBAdvertisementDataLocalNameKey: 'SM Monkey' + (' ' + self.device_id if self.device_id else ''),
        })

    def _advertising_started(self, manager, error):
        if self._closed:
            return
        if error is not None:
            self._fail(OSError('Mac 蓝牙广播失败：' + str(error)))
        else:
            self._report('Mac 蓝牙已就绪，请在 Windows 端扫描并连接这台 Mac（无需输入 IP）')

    @staticmethod
    def _id(central):
        return str(central.identifier().UUIDString())

    @staticmethod
    def _uuid(characteristic):
        return str(characteristic.UUID().UUIDString()).lower()

    def _subscribed(self, manager, central, characteristic):
        from .bluetooth_transport import BleStream, TX_UUID
        if self._closed or self._uuid(characteristic) != TX_UUID:
            return
        with self._condition:
            if self._closed or self._central_id is not None:
                return  # Never broadcast the encrypted session to another peer.
            self._central = central
            self._central_id = self._id(central)
            self._mtu = max(1, min(512, int(central.maximumUpdateValueLength()) or 20))
            self._stream = BleStream(writer=self._write, on_close=self.stop)
            self._condition.notify_all()
        manager.stopAdvertising()
        if hasattr(manager, 'setDesiredConnectionLatency_forCentral_'):
            CB, _, _ = self._native
            manager.setDesiredConnectionLatency_forCentral_(
                CB.CBPeripheralManagerConnectionLatencyLow, central)
        self._report('蓝牙通道已连接，正在验证连接密码…')

    def _unsubscribed(self, manager, central, characteristic):
        from .bluetooth_transport import TX_UUID
        if (self._uuid(characteristic) == TX_UUID and
                self._id(central) == self._central_id):
            self._fail(ConnectionError('Windows 已断开蓝牙连接，请重新连接'))

    def _write_requests(self, manager, requests):
        from .bluetooth_transport import RX_UUID
        if not requests:
            return
        CB, _, _ = self._native
        payload = []
        result = CB.CBATTErrorSuccess
        for request in requests:
            if (self._closed or self._stream is None or
                    self._id(request.central()) != self._central_id):
                result = CB.CBATTErrorInsufficientAuthorization
                break
            if self._uuid(request.characteristic()) != RX_UUID:
                result = CB.CBATTErrorWriteNotPermitted
                break
            if int(request.offset()) != 0:
                result = CB.CBATTErrorInvalidOffset
                break
            payload.append(bytes(request.value() or b''))
        if result == CB.CBATTErrorSuccess:
            try:
                # Validate the complete ATT request batch before accepting any of it.
                self._stream.feed(b''.join(payload))
                if self._stream.closed:
                    result = CB.CBATTErrorInsufficientResources
            except Exception as exc:
                result = CB.CBATTErrorInsufficientResources
                self._fail(OSError('蓝牙接收缓冲区已满：' + str(exc)))
        # Apple requires exactly one response, using the first request of a batch.
        manager.respondToRequest_withResult_(requests[0], result)

    def _read_request(self, manager, request):
        from .bluetooth_transport import INFO_UUID
        CB, F, _ = self._native
        if self._uuid(request.characteristic()) != INFO_UUID:
            result = CB.CBATTErrorReadNotPermitted
        elif int(request.offset()) > len(self._info_bytes):
            result = CB.CBATTErrorInvalidOffset
        else:
            value = self._info_bytes[int(request.offset()):]
            request.setValue_(F.NSData.dataWithBytes_length_(value, len(value)))
            result = CB.CBATTErrorSuccess
        manager.respondToRequest_withResult_(request, result)

    def _check_open(self):
        if self._error is not None:
            raise self._error
        if self._closed or self._stream is None:
            raise ConnectionError('Mac 蓝牙连接已关闭')

    def _write(self, data, timeout):
        """Wait off the main queue until Core Bluetooth accepts every byte."""
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError('Bluetooth writes must run on a worker thread')
        deadline = None if timeout is None else time.monotonic() + timeout
        data = bytes(data)
        with self._write_lock:
            for index in range(0, len(data), self.BUFFER_LIMIT):
                batch = _Batch(data[index:index + self.BUFFER_LIMIT])
                with self._condition:
                    self._check_open()
                    self._pending.append(batch)
                    self._queued_bytes += len(batch.data)
                self._dispatch(self._drain)
                with self._condition:
                    while not batch.done:
                        self._check_open()
                        remaining = None if deadline is None else deadline - time.monotonic()
                        if remaining is not None and remaining <= 0:
                            error = TimeoutError('蓝牙发送超时，请重新连接')
                            break
                        self._condition.wait(remaining)
                    else:
                        continue
                self._fail(error)
                raise error

    def _drain(self):
        """Main-queue only. NO from updateValue preserves the exact pending chunk."""
        _, F, _ = self._native
        with self._condition:
            if self._closed or self._error is not None or self._manager is None:
                return
            while self._pending:
                batch = self._pending[0]
                # CBCentral reports the currently negotiated notification
                # value length. It can grow after subscription; retrying a
                # backpressured batch must use that current limit instead of
                # retaining a 20-byte subscription-time snapshot forever.
                self._mtu = max(1, min(512, int(self._central.maximumUpdateValueLength()) or 20))
                chunk = batch.data[batch.offset:batch.offset + self._mtu]
                value = F.NSData.dataWithBytes_length_(chunk, len(chunk))
                if not self._manager.updateValue_forCharacteristic_onSubscribedCentrals_(
                        value, self._tx, [self._central]):
                    return  # Resume only when Apple's ready-to-update callback arrives.
                batch.offset += len(chunk)
                self._queued_bytes -= len(chunk)
                if batch.offset == len(batch.data):
                    batch.done = True
                    self._pending.popleft()
                    self._condition.notify_all()

    def _fail(self, error):
        with self._condition:
            if self._closed:
                return
            if self._error is None:
                self._error = error
            stream = self._stream
            self._condition.notify_all()
        if stream is not None:
            stream.finish(error)
        self.stop()

    def stop(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            stream = self._stream
            self._pending.clear()
            self._queued_bytes = 0
            self._condition.notify_all()
        if stream is not None:
            stream.finish(self._error)
        if self._native is not None:
            self._dispatch(self._stop_native)
        else:
            self._cleanup_done.set()

    close = stop

    @property
    def cleanup_pending(self):
        return self._closed and not self._cleanup_done.is_set()

    def _stop_native(self):
        manager, self._manager = self._manager, None
        if manager is not None:
            for operation in (manager.stopAdvertising, manager.removeAllServices,
                              lambda: manager.setDelegate_(None)):
                try:
                    operation()
                except Exception:
                    # Radio resets can invalidate the manager mid-cleanup;
                    # still detach the delegate and release all owned objects.
                    pass
        if self._delegate is not None:
            self._delegate.owner = None
        self._delegate = None
        self._central = None
        self._service = self._rx = self._tx = self._info = None
        self._cleanup_done.set()
