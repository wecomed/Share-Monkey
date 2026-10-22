"""Small, independently schedulable BLE clipboard messages.

The transport preserves byte order; this assembler validates clipboard transfer
order and updates the local clipboard only after receiving the complete UTF-8
text. Encoding does not modify the receive state, so opposite directions can
transfer concurrently.
"""
import base64
import binascii
import math
import secrets
import time


class ClipboardChunks:
    def __init__(self, limit=8192, chunk_size=128, timeout=30.0, clock=time.monotonic):
        if type(limit) is not int or limit < 1:
            raise ValueError('蓝牙剪贴板长度上限无效')
        if type(chunk_size) is not int or chunk_size < 1:
            raise ValueError('蓝牙剪贴板分块长度无效')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('蓝牙剪贴板超时无效')
        if not callable(clock):
            raise ValueError('蓝牙剪贴板时钟无效')
        self.limit = limit
        self.chunk_size = chunk_size
        self.max_chunks = max(1, (limit + chunk_size - 1) // chunk_size)
        self.timeout, self.clock = timeout, clock
        self._reset()

    def _reset(self):
        self._transfer = None
        self._total = self._next = 0
        self._data = bytearray()
        self._deadline = 0

    def encode(self, text):
        if not isinstance(text, str):
            raise ValueError('剪贴板内容必须是文本')
        try:
            raw = text.encode('utf-8')
        except UnicodeError as exc:
            raise ValueError('剪贴板文本不是有效的 UTF-8') from exc
        if len(raw) > self.limit:
            raise ValueError('剪贴板内容超过蓝牙长度上限')
        transfer = secrets.token_hex(16)
        total = max(1, (len(raw) + self.chunk_size - 1) // self.chunk_size)
        return [dict(type='clipboard_part', transfer=transfer, index=index, total=total,
                     data=base64.b64encode(raw[index * self.chunk_size:(index + 1) * self.chunk_size]).decode('ascii'))
                for index in range(total)]

    def receive(self, message):
        """Return complete text (including ''), or None while still assembling.

        A valid index-zero message replaces an incomplete older clipboard. Any
        invalid/out-of-order message discards partial state and raises ValueError.
        A transfer expires after 30 seconds without an accepted chunk.
        """
        try:
            return self._receive(message)
        except ValueError:
            self._reset()
            raise

    def _receive(self, message):
        if not isinstance(message, dict) or message.get('type') != 'clipboard_part':
            raise ValueError('蓝牙剪贴板消息无效')
        transfer, index, total, encoded = (message.get(key) for key in ('transfer', 'index', 'total', 'data'))
        if (not isinstance(transfer, str) or len(transfer) != 32
                or any(char not in '0123456789abcdef' for char in transfer)):
            raise ValueError('蓝牙剪贴板传输编号无效')
        if (type(index) is not int or type(total) is not int
                or not 1 <= total <= self.max_chunks or not 0 <= index < total):
            raise ValueError('蓝牙剪贴板分块编号无效')
        max_encoded = ((min(self.chunk_size, self.limit) + 2) // 3) * 4
        if not isinstance(encoded, str) or len(encoded) > max_encoded:
            raise ValueError('蓝牙剪贴板分块过大或格式无效')
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('蓝牙剪贴板编码无效') from exc
        if base64.b64encode(chunk).decode('ascii') != encoded:
            raise ValueError('蓝牙剪贴板编码无效')
        if (len(chunk) > self.chunk_size or len(chunk) > self.limit
                or (index < total - 1 and len(chunk) != self.chunk_size)
                or (total > 1 and not chunk)):
            raise ValueError('蓝牙剪贴板分块长度无效')
        now = self.clock()
        if self._transfer is not None and now >= self._deadline:
            self._reset()
        if index == 0:
            if transfer == self._transfer:
                raise ValueError('蓝牙剪贴板分块重复，请重新复制文本')
            self._reset()
            self._transfer, self._total = transfer, total
        elif (transfer != self._transfer or total != self._total or index != self._next):
            raise ValueError('蓝牙剪贴板分块顺序无效，请重新复制文本')
        if index != self._next or total != self._total:
            raise ValueError('蓝牙剪贴板分块顺序无效，请重新复制文本')
        if len(self._data) + len(chunk) > self.limit:
            raise ValueError('蓝牙剪贴板内容过大')
        self._data.extend(chunk)
        self._next += 1
        self._deadline = now + self.timeout
        if self._next < total:
            return None
        raw = bytes(self._data)
        self._reset()
        try:
            return raw.decode('utf-8')
        except UnicodeError as exc:
            raise ValueError('蓝牙剪贴板文本不是有效的 UTF-8') from exc
