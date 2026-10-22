"""IPv4 LAN discovery; addresses always come from the received UDP packet."""
from dataclasses import dataclass
import ipaddress
import json
import secrets
import socket
import sys
import threading
import time

DISCOVERY_PORT = 24810
PROTOCOL = 'sharemouse-lab-discovery-v1'
MAX_PACKET = 1024
PEER_TTL = 7


def packet(kind, nonce, **fields):
    return json.dumps(dict(protocol=PROTOCOL, type=kind, nonce=nonce, **fields)).encode()


def decode(raw):
    if len(raw) > MAX_PACKET:
        raise ValueError('packet too large')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('protocol') != PROTOCOL:
        raise ValueError('unknown protocol')
    nonce = value.get('nonce')
    if not isinstance(nonce, str) or len(nonce) != 32:
        raise ValueError('invalid nonce')
    return value


@dataclass(frozen=True)
class Peer:
    host: str
    port: int
    name: str
    platform: str

    @property
    def label(self):
        return f'{self.name} · {self.platform} ({self.host})'


def parse_peer(raw, address, nonce):
    value = decode(raw)
    if value.get('type') != 'offer' or value['nonce'] != nonce:
        raise ValueError('unsolicited response')
    port = value.get('port')
    name = value.get('name')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('invalid port')
    if not isinstance(name, str) or not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
        raise ValueError('invalid device name')
    platform = value.get('platform')
    if platform not in ('Mac', 'Windows'):
        raise ValueError('invalid platform')
    host = str(ipaddress.IPv4Address(address[0]))
    return Peer(host, port, name, platform)


class Worker:
    def __init__(self):
        self.done = threading.Event()
        self.sock = None

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self

    def stop(self):
        self.done.set()
        if self.sock:
            self.sock.close()


class Advertiser(Worker):
    def __init__(self, tcp_port, error=lambda message: None, bind='0.0.0.0', udp_port=DISCOVERY_PORT):
        super().__init__()
        self.tcp_port, self.error = tcp_port, error
        self.bind, self.udp_port = bind, udp_port
        self.name = ''.join(c for c in socket.gethostname() if ord(c) >= 32)[:80] or 'Computer'

    def run(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind((self.bind, self.udp_port))
            self.sock.settimeout(.5)
            last_reply = {}  # Bound response rate per sender.
            while not self.done.is_set():
                try:
                    raw, address = self.sock.recvfrom(MAX_PACKET + 1)
                except socket.timeout:
                    continue
                try:
                    query = decode(raw)
                    now = time.monotonic()
                    if query.get('type') != 'query' or now - last_reply.get(address[0], -1) < .5:
                        continue
                    if len(last_reply) >= 256:
                        last_reply.clear()
                    last_reply[address[0]] = now
                    self.sock.sendto(packet('offer', query['nonce'], name=self.name,
                                            platform='Windows' if sys.platform == 'win32' else 'Mac',
                                            port=self.tcp_port), address)
                except (ValueError, TypeError, UnicodeError):
                    continue
        except OSError as exc:
            if not self.done.is_set():
                self.error('自动发现不可用，可使用手动连接：' + str(exc))
        finally:
            self.stop()


class Browser(Worker):
    def __init__(self, update, error=lambda message: None, target='255.255.255.255', udp_port=DISCOVERY_PORT):
        super().__init__()
        self.update, self.error = update, error
        self.target, self.udp_port = target, udp_port

    def run(self):
        peers = {}
        nonce = secrets.token_hex(16)
        next_query = 0
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.sock.bind(('0.0.0.0', 0))
            self.sock.settimeout(.3)
            while not self.done.is_set():
                now = time.monotonic()
                if now >= next_query:
                    self.sock.sendto(packet('query', nonce), (self.target, self.udp_port))
                    next_query = now + 2
                expired = [key for key, (_, seen) in peers.items() if now - seen > PEER_TTL]
                for key in expired:
                    del peers[key]
                if expired:
                    self.update([peer for peer, _ in peers.values()])
                try:
                    raw, address = self.sock.recvfrom(MAX_PACKET + 1)
                except socket.timeout:
                    continue
                try:
                    peer = parse_peer(raw, address, nonce)
                    key = (peer.host, peer.port)
                    changed = key not in peers or peers[key][0] != peer
                    if key not in peers and len(peers) >= 128:
                        continue
                    peers[key] = (peer, time.monotonic())
                    if changed:
                        self.update([p for p, _ in peers.values()])
                except (ValueError, TypeError, UnicodeError):
                    continue
        except OSError as exc:
            if not self.done.is_set():
                self.error('搜索设备失败，可使用手动连接：' + str(exc))
        finally:
            self.stop()
