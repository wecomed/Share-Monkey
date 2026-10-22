from dataclasses import dataclass
from pathlib import Path
import base64
import hashlib
import math
import os
import uuid


@dataclass
class Screen:
    width: int
    height: int
    scale: float = 1.0

    def __post_init__(self):
        if not 100 <= self.width <= 32768 or not 100 <= self.height <= 32768:
            raise ValueError('屏幕尺寸无效')
        if not math.isfinite(self.scale) or not .5 <= self.scale <= 8:
            raise ValueError('屏幕缩放无效')

    def clamp(self, x, y):
        return max(0, min(self.width - 1, x)), max(0, min(self.height - 1, y))


class Cursor:
    def __init__(self, screen, side, return_margin=0):
        self.screen, self.side = screen, side
        self.x, self.y = 0, 0
        self.return_margin = return_margin
        self.return_pressure = 0

    def enter(self, fraction):
        self.x = 2 if self.side == 'right' else self.screen.width - 3
        self.y = round(max(0, min(1, fraction)) * (self.screen.height - 1))
        return self.x, self.y

    def move(self, dx, dy):
        if self.return_margin:
            # Absorb a small wrist rebound at the entry seam. A deliberate
            # outward gesture still returns immediately after this distance.
            toward = -dx if self.side == 'right' else dx
            distance = self.x if self.side == 'right' else self.screen.width - 1 - self.x
            if toward <= 0:
                self.return_pressure = 0
            elif toward > distance:
                pressure = self.return_pressure + toward - distance
                if pressure >= self.return_margin:
                    fraction = min(1, (distance + self.return_margin - self.return_pressure) / toward)
                    edge = 0 if self.side == 'right' else self.screen.width - 1
                    self.x, self.y = self.screen.clamp(edge, self.y + dy * fraction)
                    self.return_pressure = 0
                    return True, (self.x, self.y)
                self.return_pressure = pressure
            self.x, self.y = self.screen.clamp(self.x + dx, self.y + dy)
            return False, (self.x, self.y)
        x = self.x + dx
        leave = x < 0 if self.side == 'right' else x >= self.screen.width
        self.x, self.y = self.screen.clamp(x, self.y + dy)
        return leave, (self.x, self.y)


class FileReceiver:
    """One bounded, explicitly approved transfer, atomically committed on checksum match."""
    LIMIT = 100 * 1024 * 1024

    def __init__(self, directory):
        self.directory = Path(directory)
        self.handle = None

    def begin(self, message):
        self.abort()
        name = message['name']
        if not isinstance(name, str) or not name or '/' in name or '\\' in name or ':' in name or name in ('.', '..'):
            raise ValueError('文件名无效')
        size = message['size']
        if type(size) is not int or not 0 <= size <= self.LIMIT:
            raise ValueError('文件上限为 100 MB')
        self.directory.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex[:10]
        # Prefix every name: Windows device names and existing paths cannot collide.
        self.destination = self.directory / (suffix + '_' + name)
        self.temp = self.directory / (suffix + '.part')
        self.handle = self.temp.open('xb')
        self.expected, self.total = size, 0
        self.digest = hashlib.sha256()

    def chunk(self, data):
        if self.handle is None:
            raise ValueError('尚未接受文件')
        raw = base64.b64decode(data, validate=True)
        if len(raw) > 65536 or self.total + len(raw) > self.expected:
            raise ValueError('文件数据长度无效')
        self.handle.write(raw)
        self.total += len(raw)
        self.digest.update(raw)

    def finish(self, checksum):
        if self.handle is None or self.total != self.expected or self.digest.hexdigest() != checksum:
            self.abort()
            raise ValueError('文件校验失败')
        self.handle.close()
        self.handle = None
        os.replace(self.temp, self.destination)
        return self.destination

    def abort(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None
            self.temp.unlink(missing_ok=True)
