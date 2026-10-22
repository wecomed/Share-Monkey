"""LAN Fernet frames and small BLE v2 AES-GCM frames with independent keys."""
import base64
import json
import os
import struct
import threading

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import compact_codec

MAX_FRAME = 2 * 1024 * 1024
MAX_BLE_FRAME = 32 * 1024
MAX_BLE_PLAIN = 64 * 1024
MAGIC = b'SMLAB001'
BLE_MAGIC = b'SMBLE002'


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError('对方已断开连接')
        data.extend(chunk)
    return bytes(data)


class Channel:
    def __init__(self, sock, password, salt, server, compact=False):
        if len(password) < 4:
            raise ValueError('配对密码至少需要 4 个字符，支持 4 位数字')
        material = PBKDF2HMAC(algorithm=hashes.SHA256(), length=64,
                             salt=salt, iterations=480000).derive(password.encode())
        first, second = ((AESGCM(material[i:i + 32]) if compact else
                          Fernet(base64.urlsafe_b64encode(material[i:i + 32]))) for i in (0, 32))
        self.tx, self.rx = (first, second) if server else (second, first)
        self.sock = sock
        self.sent = self.received = 0
        self.lock = threading.Lock()
        self.rx_lock = threading.Lock()
        self.failed = False
        self.compact = compact
        self.tx_direction, self.rx_direction = (b'S', b'C') if server else (b'C', b'S')
        self.frame_limit = MAX_BLE_FRAME if compact else MAX_FRAME

    @classmethod
    def handshake(cls, sock, password, server, compact=False):
        if compact:
            # Both sides contribute fresh entropy: replaying a server nonce
            # must never force a client to reuse an AES-GCM key/nonce pair.
            if server:
                server_nonce = os.urandom(32)
                sock.sendall(BLE_MAGIC + server_nonce)
                if read_exact(sock, len(BLE_MAGIC)) != BLE_MAGIC:
                    raise ValueError('蓝牙协议已升级，请将两端更新到 v0.5.2')
                client_nonce = read_exact(sock, 32)
            else:
                if read_exact(sock, len(BLE_MAGIC)) != BLE_MAGIC:
                    raise ValueError('蓝牙协议已升级，请将两端更新到 v0.5.2')
                server_nonce = read_exact(sock, 32)
                client_nonce = os.urandom(32)
                sock.sendall(BLE_MAGIC + client_nonce)
            salt = b'ShareMonkeyBLE2' + server_nonce + client_nonce
        elif server:
            salt = os.urandom(32)
            sock.sendall(MAGIC + salt)
        else:
            if read_exact(sock, len(MAGIC)) != MAGIC:
                raise ValueError('协议版本不匹配')
            salt = read_exact(sock, 32)
        channel = cls(sock, password, salt, server, compact=compact)
        if server:
            channel.send({'type': 'hello'})
            if channel.recv() != {'type': 'ready'}:
                raise ValueError('配对失败')
        else:
            if channel.recv() != {'type': 'hello'}:
                raise ValueError('配对失败')
            channel.send({'type': 'ready'})
        return channel

    def send(self, message):
        with self.lock:
            if self.compact:
                if self.failed:
                    raise ConnectionError('蓝牙加密通道已关闭，请重新连接')
                try:
                    if not 0 <= self.sent < 2 ** 64:
                        raise ValueError('蓝牙会话计数已耗尽，请重新连接')
                    raw = compact_codec.encode(message)
                    size = len(raw) + 16
                    if not 0 < size <= self.frame_limit:
                        raise ValueError('蓝牙消息过大')
                    header = struct.pack('!I', size)
                    seq = struct.pack('!Q', self.sent)
                    token = self.tx.encrypt(b'SMB2' + seq, raw, BLE_MAGIC + self.tx_direction + header + seq)
                    self.sock.sendall(header + token)
                    self.sent += 1
                except Exception:
                    # A failed write may have emitted part or all of its frame.
                    # Poison rather than retrying a nonce or losing framing.
                    self.failed = True
                    raise
                return
            raw = json.dumps({'seq': self.sent, 'body': message}, ensure_ascii=False).encode()
            token = self.tx.encrypt(raw)
            if len(token) > self.frame_limit:
                raise ValueError('消息过大')
            self.sock.sendall(struct.pack('!I', len(token)) + token)
            self.sent += 1

    def recv(self):
        if self.compact:
            with self.rx_lock:
                if self.failed:
                    raise ConnectionError('蓝牙加密通道已关闭，请重新连接')
                try:
                    if not 0 <= self.received < 2 ** 64:
                        raise ValueError('蓝牙会话计数已耗尽，请重新连接')
                    header = read_exact(self.sock, 4)
                    size, = struct.unpack('!I', header)
                    if not 16 < size <= self.frame_limit:
                        raise ValueError('消息长度无效')
                    seq = struct.pack('!Q', self.received)
                    raw = self.rx.decrypt(b'SMB2' + seq, read_exact(self.sock, size),
                                          BLE_MAGIC + self.rx_direction + header + seq)
                    message = compact_codec.decode(raw)
                    self.received += 1
                    return message
                except Exception:
                    self.failed = True
                    raise
        size, = struct.unpack('!I', read_exact(self.sock, 4))
        if not 0 < size <= self.frame_limit:
            raise ValueError('消息长度无效')
        plain = self.rx.decrypt(read_exact(self.sock, size))
        envelope = json.loads(plain)
        if envelope['seq'] != self.received or not isinstance(envelope['body'], dict):
            raise ValueError('消息顺序无效')
        self.received += 1
        return envelope['body']
