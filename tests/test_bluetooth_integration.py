"""Real encrypted BLE sessions without radios, native input, GUI, or IP sockets.

Only the platform endpoints and physical input/clipboard are replaced. Engine.run, writer,
motion coalescing, Channel authentication/encryption, and BleStream are real.
The fake GATT wire deliberately fragments length headers as well as payloads.
"""
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.exceptions import InvalidTag
from sharemouse import compact_codec
from sharemouse.bluetooth_clipboard import ClipboardChunks
from sharemouse.bluetooth_transport import BleStream, CLIPBOARD_LIMIT
from sharemouse.core import Screen
from sharemouse.engine import Engine
from sharemouse.protocol import BLE_MAGIC, Channel, MAX_BLE_FRAME, MAX_BLE_PLAIN


PASSWORD = 'bluetooth-integration-password'


class FragmentedWire:
    """Ordered GATT notifications/writes with a negotiated payload ceiling."""

    def __init__(self, mtu):
        self.mtu = mtu
        self.sent = [[], []]
        self.fragments = [[], []]
        self.streams = [BleStream(timeout=2), BleStream(timeout=2)]
        for index, stream in enumerate(self.streams):
            stream.set_writer(lambda data, timeout, i=index: self.write(i, data))
            stream.on_close = self.close

    def write(self, index, data):
        self.sent[index].append(bytes(data))
        # The first two tiny chunks ensure read_exact cannot assume that even
        # the four-byte length field arrives in a single characteristic value.
        position = 0
        for size in (1, 2):
            if position < len(data):
                fragment = bytes(data[position:position + size])
                self.fragments[index].append(fragment)
                self.streams[1 - index].feed(fragment)
                position += len(fragment)
        while position < len(data):
            fragment = bytes(data[position:position + self.mtu])
            self.fragments[index].append(fragment)
            self.streams[1 - index].feed(fragment)
            position += len(fragment)

    def close(self):
        for stream in self.streams:
            stream.finish()

    def fail(self):
        for stream in self.streams:
            stream.finish(ConnectionError('模拟蓝牙距离过远'))


class RecordingInput:
    def __init__(self):
        self.listeners = []
        self.keys = set()
        self.buttons = set()
        self.release_count = 0
        self.stop_count = 0
        self.positions = []
        self.restores = []
        self.injected_keys = []
        self.remote_active = False
        self.mouse = Mock()

    def set_remote_active(self, active):
        self.remote_active = active

    def release(self):
        self.release_count += 1
        self.keys.clear()
        self.buttons.clear()

    def stop(self):
        self.stop_count += 1
        self.release()

    def move_to(self, position):
        self.positions.append(position)

    def restore_local(self, x, y):
        self.restores.append((x, y))

    def inject_key(self, key, down):
        self.injected_keys.append((key, down))
        value = json.dumps(key, sort_keys=True)
        (self.keys.add if down else self.keys.discard)(value)

    def inject_button(self, button, down):
        (self.buttons.add if down else self.buttons.discard)(button)


class ThreadPlatform:
    """Engine's platform decision is per run thread, never process-global."""

    def __init__(self):
        self.local = threading.local()

    @property
    def platform(self):
        return getattr(self.local, 'platform', sys.platform)


class EnginePair:
    def __init__(self, case, mtu=20, mac_controller=True,
                 windows_controller=None, windows_password=PASSWORD):
        self.case = case
        self.wire = FragmentedWire(mtu)
        self.platform = ThreadPlatform()
        self.status = [[], []]
        self.channels = {}
        self.writers = []
        self.patchers = []
        self.closed = False
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        windows_controller = not mac_controller if windows_controller is None else windows_controller
        self.mac = Engine(mac_controller, Screen(1728, 1117, 2), 'right', PASSWORD,
                          '', 24809, self.status[0].append, clipboard=False, files=True,
                          directory=self.root / 'mac', transport='bluetooth')
        self.windows = Engine(windows_controller, Screen(2560, 1440, 1.5), 'right',
                              windows_password, 'AA:BB:CC:DD:EE:FF', 24809,
                              self.status[1].append, clipboard=False, files=True,
                              directory=self.root / 'windows', transport='bluetooth')
        self.engines = (self.mac, self.windows)
        for engine in self.engines:
            engine.input = RecordingInput()
            # The clipboard itself is an OS service, just like keyboard hooks.
            # Even when testing inbound clipboard permission, never start a
            # poller that might read the host computer's actual clipboard.
            engine.clipboard_loop = Mock()
            original_writer = engine.writer

            def writer(channel, function=original_writer):
                self.writers.append(threading.current_thread())
                function(channel)

            engine.writer = writer
        self.mac_backend = Mock()
        self.mac_backend.accept.return_value = self.wire.streams[0]
        self.windows_backend = Mock()
        self.windows_backend.connect.return_value = self.wire.streams[1]
        handshake = Channel.handshake

        def capture(stream, password, server, compact=False):
            channel = handshake(stream, password, server, compact=compact)
            self.channels[stream] = (channel, server, compact)
            return channel

        self.mac_factory = self.start_patch('sharemouse.bluetooth_mac.MacBleServer',
                                          return_value=self.mac_backend)
        self.windows_factory = self.start_patch('sharemouse.bluetooth_windows.WindowsBleConnector',
                                              return_value=self.windows_backend)
        self.start_patch('sharemouse.engine.sys', self.platform)
        self.start_patch('sharemouse.engine.Channel.handshake', side_effect=capture)
        self.network_connect = self.start_patch('sharemouse.engine.socket.create_connection',
                                               side_effect=AssertionError('BLE opened an IP connection'))
        self.network_listener = self.start_patch('sharemouse.engine.socket.socket',
                                                side_effect=AssertionError('BLE opened an IP listener'))
        self.advertiser = self.start_patch('sharemouse.engine.Advertiser',
                                         side_effect=AssertionError('BLE started LAN discovery'))
        self.threads = [threading.Thread(target=self.run, args=(engine, platform), daemon=True)
                        for engine, platform in zip(self.engines, ('darwin', 'win32'))]
        case.addCleanup(self.close)

    def start_patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        self.patchers.append(patcher)
        return patcher.start()

    def run(self, engine, platform):
        self.platform.local.platform = platform
        engine.run()

    def start(self, connected=True):
        for thread in self.threads:
            thread.start()
        if connected:
            self.case.wait_for(lambda: all(e.connected for e in self.engines)
                               and len(self.writers) == 2, self)
        return self

    def assert_no_network(self):
        self.network_connect.assert_not_called()
        self.network_listener.assert_not_called()
        self.advertiser.assert_not_called()

    def close(self):
        if self.closed:
            return
        self.closed = True
        leaked = []
        try:
            for engine in self.engines:
                engine.stop()
            self.wire.close()
            for thread in self.threads + self.writers:
                if thread.ident is not None:
                    thread.join(3)
                    if thread.is_alive():
                        leaked.append(thread.name)
        finally:
            for patcher in reversed(self.patchers):
                patcher.stop()
            self.directory.cleanup()
        self.case.assertEqual(leaked, [], 'BLE session worker leaked')


class BluetoothEngineIntegrationTests(unittest.TestCase):
    def wait_for(self, predicate, pair):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.001)
        self.fail('Timed out waiting for BLE session: ' + repr(pair.status))

    def pair(self, **kwargs):
        return EnginePair(self, **kwargs).start()

    def test_both_control_directions_and_mtus_survive_128_round_trips(self):
        for mtu in (20, 185):
            for mac_controller in (True, False):
                with self.subTest(mtu=mtu, mac_controller=mac_controller):
                    pair = self.pair(mtu=mtu, mac_controller=mac_controller)
                    try:
                        controller, receiver = ((pair.mac, pair.windows) if mac_controller
                                                else (pair.windows, pair.mac))
                        pair.mac_factory.assert_called_once_with(pair.mac.status,
                            role='controller' if mac_controller else 'receiver')
                        pair.windows_factory.assert_called_once_with(pair.windows.host, pair.windows.status,
                                                                    metrics=pair.windows.metrics)
                        pair.mac_backend.start.assert_called_once()
                        self.assertEqual(pair.channels[pair.mac.sock][1:], (True, True))
                        self.assertEqual(pair.channels[pair.windows.sock][1:], (False, True))
                        for epoch in range(1, 129):
                            side = 'right' if epoch <= 64 else 'left'
                            toward = 1 if side == 'right' else -1
                            fraction = (epoch % 5) / 4
                            with controller.state_lock:
                                controller.side, controller.epoch = side, epoch
                                controller.remote = True
                                controller.send({'type': 'enter', 'side': side, 'fraction': fraction})
                                controller.send({'type': 'move', 'dx': 100 * toward, 'dy': 0})
                                controller.send({'type': 'key', 'key': {'char': 'a'}, 'down': True})
                            # Exercise delivered input before an intentional
                            # return; an immediate F12/edge cancellation is a
                            # separate case and may discard the unsent suffix.
                            self.wait_for(lambda: receiver.active and len(receiver.input.injected_keys) == epoch, pair)
                            with controller.state_lock:
                                controller.send({'type': 'move', 'dx': -1000 * toward, 'dy': 0})
                            self.wait_for(lambda: not controller.remote and controller.pending_return is None
                                          and receiver.received_epoch == epoch, pair)
                            self.assertTrue(controller.connected and receiver.connected)
                            self.assertFalse(receiver.active)
                            self.assertFalse(receiver.input.keys)
                            self.assertEqual(len(controller.input.restores), epoch)
                            x, y = controller.input.restores[-1]
                            self.assertEqual(x, controller.local.width - 12 if side == 'right' else 12)
                            remote_y = round(fraction * (receiver.local.height - 1))
                            self.assertAlmostEqual(y / (controller.local.height - 1),
                                                   remote_y / (receiver.local.height - 1))
                        self.assertEqual(len(receiver.input.injected_keys), 128)
                        self.assertEqual(len(pair.channels), 2, 'crossing must reuse authenticated channels')
                        pair.mac_backend.close.assert_not_called()
                        pair.windows_backend.close.assert_not_called()
                        for fragments in pair.wire.fragments:
                            self.assertGreater(len(fragments), 128)
                            self.assertTrue(all(0 < len(fragment) <= mtu for fragment in fragments))
                        pair.assert_no_network()
                    finally:
                        # Subtest patches must not overlap the next platform pair.
                        pair.close()

    def test_wrong_password_never_connects_and_releases_both_inputs(self):
        pair = EnginePair(self, windows_password='another-valid-password').start(connected=False)
        self.wait_for(lambda: all(not thread.is_alive() for thread in pair.threads), pair)
        self.assertTrue(all(not e.connected and e.stop_event.is_set() for e in pair.engines))
        self.assertTrue(all(e.input.stop_count and not e.input.keys for e in pair.engines))
        self.assertEqual(pair.writers, [])
        self.assertTrue(any('密码' in message or '连接结束' in message
                            for messages in pair.status for message in messages))
        pair.assert_no_network()

    def test_same_controller_or_same_receiver_is_rejected_after_authentication(self):
        for role in (True, False):
            with self.subTest(controller=role):
                pair = EnginePair(self, mac_controller=role, windows_controller=role).start(connected=False)
                try:
                    self.wait_for(lambda: all(not thread.is_alive() for thread in pair.threads), pair)
                    self.assertTrue(all(not e.connected and e.input.stop_count for e in pair.engines))
                    self.assertEqual(pair.writers, [])
                    self.assertTrue(any('两端角色相同' in message
                                        for messages in pair.status for message in messages))
                    pair.assert_no_network()
                finally:
                    pair.close()

    def test_radio_disconnect_while_remote_restores_local_and_releases_held_keys(self):
        pair = self.pair()
        controller, receiver = pair.mac, pair.windows
        controller.remote, controller.epoch = True, 7
        controller.send({'type': 'enter', 'side': 'right', 'fraction': .5})
        controller.send({'type': 'key', 'key': {'special': 'ctrl'}, 'down': True})
        self.wait_for(lambda: bool(receiver.input.keys), pair)
        pair.wire.fail()
        self.wait_for(lambda: all(not thread.is_alive() for thread in pair.threads), pair)
        self.assertFalse(controller.remote)
        self.assertFalse(receiver.active)
        self.assertEqual(len(controller.input.restores), 1)
        self.assertTrue(all(e.input.stop_count and not e.input.keys for e in pair.engines))
        self.assertTrue(pair.wire.streams[0].closed and pair.wire.streams[1].closed)

    def test_files_are_blocked_even_when_requested_in_settings_or_by_peer(self):
        pair = self.pair()
        for engine in pair.engines:
            self.assertFalse(engine.files_enabled)
            engine.send_file(pair.root / 'does-not-exist')
        self.assertTrue(all(any('发送文件请切换为局域网' in message for message in messages)
                            for messages in pair.status))
        pair.mac.send({'type': 'file_offer', 'name': 'blocked.txt', 'size': 0})
        self.wait_for(pair.mac.file_reply.is_set, pair)
        self.assertFalse(pair.mac.file_accepted)
        self.assertFalse((pair.root / 'mac').exists())
        self.assertFalse((pair.root / 'windows').exists())
        self.assertTrue(all(e.connected for e in pair.engines))

    def test_bidirectional_clipboard_utf8_limit_and_oversize_disconnect(self):
        pair = self.pair(mtu=20)
        # Only the real inbound handler runs; no global clipboard is polled.
        for engine in pair.engines:
            engine.clipboard_enabled = True
        text = '猴' * 2730 + 'ab'
        self.assertEqual(len(text.encode()), CLIPBOARD_LIMIT)
        copied = []
        with patch('pyperclip.copy', side_effect=copied.append):
            for sender, receiver in ((pair.mac, pair.windows), (pair.windows, pair.mac)):
                expected = len(copied) + 1
                self.assertTrue(sender.queue_clipboard(text))
                self.wait_for(lambda: len(copied) == expected, pair)
                self.assertEqual(receiver.last_clipboard, text)
                self.assertTrue(sender.background_outgoing.empty())
            pair.mac.send({'type': 'clipboard', 'text': '猴' * 2731})
            self.wait_for(lambda: all(not thread.is_alive() for thread in pair.threads), pair)
        self.assertEqual(copied, [text, text])
        self.assertTrue(any('剪贴板内容过大' in message for message in pair.status[1]))

    def test_outbound_clipboard_limit_counts_utf8_bytes_and_skips_oversize(self):
        status = []
        engine = Engine(True, Screen(1920, 1080), 'right', PASSWORD, '', 24809,
                        status.append, transport='bluetooth')
        engine.connected = True
        # Drive three polling iterations deterministically, without sleeping or
        # running background threads. Initial clipboard contents stay local.
        engine.stop_event = Mock()
        engine.stop_event.wait.side_effect = (False, False, True)
        boundary = '猴' * 2730 + 'ab'
        oversized = '猴' * 2731
        with patch('pyperclip.paste', side_effect=('initial local text', boundary, oversized)):
            engine.clipboard_loop()
        self.assertEqual(engine.background_outgoing.qsize(), engine.clipboard_chunks.max_chunks)
        decoder, received = ClipboardChunks(chunk_size=engine.clipboard_chunks.chunk_size), []
        while not engine.background_outgoing.empty():
            value = decoder.receive(engine.background_outgoing.get_nowait())
            if value is not None:
                received.append(value)
        self.assertEqual(received, [boundary])
        self.assertTrue(engine.outgoing.empty())
        self.assertTrue(engine.write_wake.is_set())
        self.assertTrue(any('超过蓝牙 8 KB 上限' in message for message in status))

    def test_input_preempts_every_clipboard_fragment_on_actual_encrypted_wire(self):
        pair = self.pair(mtu=20)
        controller, receiver = pair.mac, pair.windows
        receiver.clipboard_enabled = True
        controller.remote, controller.epoch = True, 1
        controller.send({'type': 'enter', 'side': 'right', 'fraction': .5})
        self.wait_for(lambda: receiver.active, pair)
        channel = pair.channels[controller.sock][0]
        original_send = channel.send
        order, copied = [], []
        finished = threading.Event()
        # Four production chunks suffice to verify every priority barrier;
        # the full 8 KiB boundary is exercised separately above. Each input
        # batch deliberately waits for the real 150 ms idle background gate.
        text = '猴' * 42 + 'ab'
        fragments = len(controller.clipboard_chunks.encode(text))

        def send_and_offer_input(message):
            original_send(message)
            kind = message['type']
            if kind != 'ping':
                order.append(kind)
            if kind == 'clipboard_part':
                if message['index'] < message['total'] - 1:
                    # Input becomes ready immediately after each background
                    # frame. A key is an ordering barrier for motion merging.
                    controller.send({'type': 'move', 'dx': 1, 'dy': 0})
                    controller.send({'type': 'key', 'key': {'char': 'a'}, 'down': True})
                    controller.send({'type': 'key', 'key': {'char': 'a'}, 'down': False})
                else:
                    finished.set()

        with patch.object(channel, 'send', side_effect=send_and_offer_input), \
                patch('pyperclip.copy', side_effect=copied.append):
            self.assertTrue(controller.queue_clipboard(text))
            self.wait_for(lambda: finished.is_set() and copied == [text], pair)
        self.assertEqual(order, ['clipboard_part', 'pointer', 'key', 'key'] * (fragments - 1)
                         + ['clipboard_part'])
        self.assertEqual(len(receiver.input.injected_keys), 2 * (fragments - 1))
        self.assertFalse(receiver.input.keys)
        self.assertAlmostEqual(receiver.cursor.x, 2 + (fragments - 1) * receiver.local.scale / controller.local.scale)
        self.assertTrue(controller.connected and receiver.connected)
        self.assertEqual(receiver.last_clipboard, text)

    def test_cancelled_unsent_enter_gets_ack_without_reconnecting_or_flashing_remote_cursor(self):
        pair = self.pair(mtu=20)
        controller, receiver = pair.mac, pair.windows
        for epoch in range(1, 31):
            with controller.state_lock:
                controller.remote, controller.epoch = True, epoch
                controller.send(dict(type='enter', side='right', fraction=.5))
                controller.send(dict(type='move', dx=-50, dy=0))
            self.wait_for(lambda: controller.pending_return is None
                          and len(controller.input.restores) == epoch, pair)
            self.assertFalse(receiver.active)
            self.assertEqual(receiver.received_epoch, epoch)
            self.assertTrue(controller.connected and receiver.connected)
        self.assertEqual(len(pair.channels), 2)
        self.assertEqual(receiver.input.positions, [])


class BluetoothCompactProtocolTests(unittest.TestCase):
    def channels(self, mtu=20, compact=True):
        wire = FragmentedWire(mtu)
        self.addCleanup(wire.close)
        salt = os.urandom(32)
        return wire, Channel(wire.streams[0], PASSWORD, salt, True, compact=compact), \
            Channel(wire.streams[1], PASSWORD, salt, False, compact=compact)

    def test_fragmented_compact_handshake_and_unicode_both_directions(self):
        for mtu in (20, 185):
            with self.subTest(mtu=mtu):
                wire = FragmentedWire(mtu)
                self.addCleanup(wire.close)
                result, errors = [], []

                def serve():
                    try:
                        result.append(Channel.handshake(wire.streams[0], PASSWORD, True, compact=True))
                    except Exception as exc:
                        errors.append(exc)
                        wire.close()

                worker = threading.Thread(target=serve, daemon=True)
                worker.start()
                try:
                    client = Channel.handshake(wire.streams[1], PASSWORD, False, compact=True)
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [])
                    server = result[0]
                    message = {'type': 'clipboard', 'text': 'Mac ⇄ Windows，猴子 🐒\n\x00'}
                    for sender, receiver in ((server, client), (client, server)):
                        sender.send(message)
                        self.assertEqual(receiver.recv(), message)
                    frame = wire.sent[0][-1]
                    seq = struct.pack('!Q', 1)
                    raw = server.tx.decrypt(b'SMB2' + seq, frame[4:],
                                            BLE_MAGIC + server.tx_direction + frame[:4] + seq)
                    self.assertEqual(compact_codec.decode(raw), message)
                finally:
                    wire.close()
                    worker.join(3)

    def test_bounded_decompression_rejects_authenticated_zip_bomb_and_trailing_data(self):
        regular = json.dumps({'type': 'ping'}).encode()
        payloads = (zlib.compress(b'x' * (MAX_BLE_PLAIN + 1)),
                    zlib.compress(regular) + b'extra',
                    zlib.compress(regular) + zlib.compress(regular),
                    zlib.compress(regular)[:-1], b'not-a-zlib-stream')
        for payload in payloads:
            with self.subTest(payload_length=len(payload)):
                wire, server, client = self.channels()
                raw = b'\x00' + payload
                header = struct.pack('!I', len(raw) + 16)
                seq = struct.pack('!Q', 0)
                token = server.tx.encrypt(b'SMB2' + seq, raw,
                                          BLE_MAGIC + server.tx_direction + header + seq)
                wire.streams[0].sendall(header + token)
                with self.assertRaisesRegex(ValueError, '蓝牙消息'):
                    client.recv()
                self.assertEqual(client.received, 0)
                self.assertTrue(client.failed)

    def test_outgoing_plaintext_limit_applies_before_compression(self):
        wire, server, _ = self.channels()
        with self.assertRaisesRegex(ValueError, '蓝牙消息过大'):
            server.send({'type': 'clipboard', 'text': 'x' * MAX_BLE_PLAIN})
        self.assertEqual(server.sent, 0)
        self.assertEqual(wire.sent[0], [])

    def test_frame_limit_is_checked_without_waiting_for_payload(self):
        wire, _, client = self.channels()
        wire.streams[0].sendall(struct.pack('!I', MAX_BLE_FRAME + 1))
        with self.assertRaisesRegex(ValueError, '消息长度无效'):
            client.recv()

    def test_replayed_encrypted_frame_is_rejected_after_fragment_reassembly(self):
        wire, server, client = self.channels()
        server.send({'type': 'ping'})
        self.assertEqual(client.recv(), {'type': 'ping'})
        wire.streams[0].sendall(wire.sent[0][-1])
        with self.assertRaises(InvalidTag):
            client.recv()
        self.assertTrue(client.failed)

    def test_disconnect_halfway_through_frame_wakes_reader(self):
        wire, _, client = self.channels()
        wire.streams[0].sendall(struct.pack('!I', 100) + b'partial')
        wire.close()
        with self.assertRaises(ConnectionError):
            client.recv()

    def test_lan_default_remains_uncompressed_and_accepts_larger_frames(self):
        wire, server, client = self.channels(mtu=185, compact=False)
        message = {'type': 'clipboard', 'text': '局域网兼容' * 10000}
        server.send(message)
        frame = wire.sent[0][-1]
        self.assertGreater(len(frame), MAX_BLE_FRAME)
        plain = server.tx.decrypt(frame[4:])
        self.assertEqual(json.loads(plain), {'seq': 0, 'body': message})
        self.assertEqual(client.recv(), message)
        client.send({'type': 'ping'})
        self.assertEqual(server.recv(), {'type': 'ping'})


if __name__ == '__main__':
    unittest.main()
