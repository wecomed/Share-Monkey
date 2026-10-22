"""Small authenticated BLE input bodies; LAN's JSON protocol is unchanged."""
import json
import base64
import binascii
import math
import struct
import zlib

MAX_PLAIN = 64 * 1024
MAX_SAMPLES = 64
MAX_EPOCH = 2 ** 32 - 1


def _epoch(message):
    value = message.get('epoch', 0)
    if type(value) is not int or not 0 <= value <= MAX_EPOCH:
        raise ValueError('输入会话编号无效')
    return value


def _number(value, limit):
    value = float(value)
    if not math.isfinite(value) or abs(value) > limit:
        raise ValueError('输入坐标无效')
    return value


def _packet(value):
    if type(value) is not int or not 0 <= value < 2 ** 32:
        raise ValueError('蓝牙交付编号无效')
    return value


def encode(message):
    if not isinstance(message, dict):
        raise ValueError('蓝牙消息格式无效')
    if '_delivery' in message:
        if message.get('type') == 'consumed':
            raise ValueError('蓝牙确认消息不能要求再次确认')
        inner = dict(message)
        packet = _packet(inner.pop('_delivery'))
        return b'\xfe' + struct.pack('!I', packet) + _encode_body(inner)
    return _encode_body(message)


def _encode_body(message):
    kind = message.get('type')
    if kind == 'consumed':
        return struct.pack('!BI', 10, _packet(message.get('packet')))
    if kind == 'clipboard_part':
        transfer, index, total, encoded = (message.get(key) for key in ('transfer', 'index', 'total', 'data'))
        if (not isinstance(transfer, str) or len(transfer) != 32
                or any(char not in '0123456789abcdef' for char in transfer)
                or type(index) is not int or type(total) is not int
                or not 1 <= total <= 8192 or not 0 <= index < total
                or not isinstance(encoded, str) or len(encoded) > 172):
            raise ValueError('蓝牙剪贴板分块无效')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('蓝牙剪贴板编码无效') from exc
        if (len(data) > 128 or base64.b64encode(data).decode('ascii') != encoded
                or not data and total != 1):
            raise ValueError('蓝牙剪贴板分块长度无效')
        return struct.pack('!B16sHH', 11, bytes.fromhex(transfer), index, total) + data
    if kind == 'move':
        points = message.get('path', [[message.get('dx'), message.get('dy')]])
        if not isinstance(points, list) or not 1 <= len(points) <= MAX_SAMPLES:
            raise ValueError('鼠标轨迹无效')
        raw = bytearray(struct.pack('!BIB', 1, _epoch(message), len(points)))
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError('鼠标轨迹无效')
            raw.extend(struct.pack('!ff', *(_number(v, 32768) for v in point)))
        return bytes(raw)
    if kind == 'enter':
        if message.get('side') not in ('left', 'right'):
            raise ValueError('屏幕方向无效')
        return struct.pack('!BIBf', 2, _epoch(message), message['side'] == 'right',
                           _number(message['fraction'], 1))
    if kind in ('leave', 'left'):
        return struct.pack('!BI', 3 if kind == 'leave' else 5, _epoch(message))
    if kind == 'return':
        return struct.pack('!BIf', 4, _epoch(message), _number(message['fraction'], 1))
    if kind == 'ping':
        return b'\x06'
    if kind == 'scroll':
        return struct.pack('!BIff', 7, _epoch(message),
                           _number(message['dx'], 100), _number(message['dy'], 100))
    if kind == 'pointer':
        return struct.pack('!BIff', 9, _epoch(message),
                           _number(message['x'], 32768), _number(message['y'], 32768))
    if kind == 'button' and message.get('button') in ('left', 'right', 'middle', 'x1', 'x2'):
        if type(message.get('down')) is not bool:
            raise ValueError('鼠标按键无效')
        button = ('left', 'right', 'middle', 'x1', 'x2').index(message['button'])
        return struct.pack('!BIBB', 8, _epoch(message), button, message['down'])
    raw = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_PLAIN:
        raise ValueError('蓝牙消息过大，请改用局域网')
    return b'\x00' + zlib.compress(raw, level=1)


def decode(raw):
    if raw and raw[0] == 254:
        if len(raw) < 6 or raw[5] == 254:
            raise ValueError('蓝牙交付封装无效')
        packet, = struct.unpack('!I', raw[1:5])
        message = _decode_body(raw[5:])
        if message.get('type') == 'consumed' or '_delivery' in message:
            raise ValueError('蓝牙确认消息不能要求再次确认')
        message['_delivery'] = packet
        return message
    return _decode_body(raw)


def _decode_body(raw):
    if not raw:
        raise ValueError('蓝牙消息为空')
    kind = raw[0]
    if kind == 10:
        if len(raw) != 5:
            raise ValueError('蓝牙确认消息长度无效')
        packet, = struct.unpack('!I', raw[1:])
        return dict(type='consumed', packet=packet)
    if kind == 11:
        if not 21 <= len(raw) <= 149:
            raise ValueError('蓝牙剪贴板分块长度无效')
        _, transfer, index, total = struct.unpack('!B16sHH', raw[:21])
        if not 1 <= total <= 8192 or not 0 <= index < total or len(raw) == 21 and total != 1:
            raise ValueError('蓝牙剪贴板分块编号无效')
        return dict(type='clipboard_part', transfer=transfer.hex(), index=index, total=total,
                    data=base64.b64encode(raw[21:]).decode('ascii'))
    if kind == 0:
        try:
            decoder = zlib.decompressobj()
            plain = decoder.decompress(raw[1:], MAX_PLAIN + 1)
            if len(plain) > MAX_PLAIN or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError('蓝牙消息解压长度无效')
            message = json.loads(plain)
        except zlib.error as exc:
            raise ValueError('蓝牙消息格式无效') from exc
        if not isinstance(message, dict):
            raise ValueError('蓝牙消息格式无效')
        return message
    try:
        if kind == 1:
            _, epoch, count = struct.unpack('!BIB', raw[:6])
            if not 1 <= count <= MAX_SAMPLES or len(raw) != 6 + count * 8:
                raise ValueError('鼠标轨迹长度无效')
            path = [list(struct.unpack_from('!ff', raw, 6 + i * 8)) for i in range(count)]
            for point in path:
                for value in point:
                    _number(value, 32768)
            if count == 1:
                return dict(type='move', epoch=epoch, dx=path[0][0], dy=path[0][1])
            return dict(type='move', epoch=epoch, path=path)
        if kind == 2:
            _, epoch, side, fraction = struct.unpack('!BIBf', raw)
            if side not in (0, 1):
                raise ValueError('屏幕方向无效')
            return dict(type='enter', epoch=epoch, side='right' if side else 'left', fraction=_number(fraction, 1))
        if kind in (3, 5):
            _, epoch = struct.unpack('!BI', raw)
            return dict(type='leave' if kind == 3 else 'left', epoch=epoch)
        if kind == 4:
            _, epoch, fraction = struct.unpack('!BIf', raw)
            return dict(type='return', epoch=epoch, fraction=_number(fraction, 1))
        if kind == 6 and len(raw) == 1:
            return dict(type='ping')
        if kind == 7:
            _, epoch, dx, dy = struct.unpack('!BIff', raw)
            return dict(type='scroll', epoch=epoch, dx=_number(dx, 100), dy=_number(dy, 100))
        if kind == 8:
            _, epoch, button, down = struct.unpack('!BIBB', raw)
            if button > 4 or down not in (0, 1):
                raise ValueError('鼠标按键无效')
            return dict(type='button', epoch=epoch,
                        button=('left', 'right', 'middle', 'x1', 'x2')[button], down=bool(down))
        if kind == 9:
            _, epoch, x, y = struct.unpack('!BIff', raw)
            return dict(type='pointer', epoch=epoch, x=_number(x, 32768), y=_number(y, 32768))
    except struct.error as exc:
        raise ValueError('蓝牙输入长度无效') from exc
    raise ValueError('未知蓝牙消息')
