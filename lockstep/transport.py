"""传输层：进程内延迟链路与 TCP 链路。

两种链路都只搬运字节：消息一律序列化后投递，两端不共享任何可变对象，
因此"双端同步"是真同步而不是同一块内存碰巧一致。
接收线程（TCP）只负责入队字节，绝不触碰模拟状态。
"""

import socket
import struct
import threading
from collections import deque


class ManualClock:
    """测试/回放用的虚拟时钟：延迟只影响送达时机，不影响模拟结果。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class LatentLink:
    """进程内延迟链路：send 的字节在 latency 秒后才可被对端 poll 到。"""

    def __init__(self, clock):
        self._clock = clock
        self.latency = 0.0
        self._inbox = deque()  # (deliver_at, bytes)
        self._peer = None
        self.closed = False
        self.peer_closed = False

    @classmethod
    def pair(cls, latency_s, clock):
        a, b = cls(clock), cls(clock)
        a._peer, b._peer = b, a
        a.latency = b.latency = latency_s
        return a, b

    def send(self, data: bytes):
        if self.closed or self._peer is None or self._peer.closed:
            return  # 对端已断：报文丢弃，由上层政策决定等待还是替代
        payload = bytes(data)  # 复制，杜绝共享可变对象
        self._peer._inbox.append((self._clock() + self.latency, payload))

    def poll(self):
        now = self._clock()
        out = []
        while self._inbox and self._inbox[0][0] <= now:
            out.append(self._inbox.popleft()[1])
        return out

    def set_latency(self, seconds):
        self.latency = seconds

    def close(self):
        """双向关闭：两端都会观察到 peer_closed。"""
        self.closed = True
        self.peer_closed = True
        if self._peer is not None:
            self._peer.peer_closed = True


class TcpLink:
    """TCP 链路：4 字节长度前缀 + 字节负载。接收线程只入队，不碰模拟。"""

    def __init__(self, sock):
        self._sock = sock
        self._q = deque()
        self._cv = threading.Condition()
        self._send_lock = threading.Lock()
        self.peer_closed = False
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    @classmethod
    def listen(cls, port, timeout=None):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(1)
        if timeout is not None:
            srv.settimeout(timeout)
        conn, _addr = srv.accept()
        bound = srv.getsockname()[1]
        srv.close()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        link = cls(conn)
        link.port = bound
        return link

    @classmethod
    def connect(cls, host, port, timeout=None):
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _reader(self):
        try:
            while True:
                hdr = self._recv_exact(4)
                if hdr is None:
                    break
                (size,) = struct.unpack("!I", hdr)
                data = self._recv_exact(size)
                if data is None:
                    break
                with self._cv:
                    self._q.append(data)
                    self._cv.notify_all()
        except OSError:
            pass
        with self._cv:
            self.peer_closed = True
            self._cv.notify_all()

    def send(self, data: bytes):
        payload = bytes(data)
        with self._send_lock:
            self._sock.sendall(struct.pack("!I", len(payload)) + payload)

    def poll(self):
        with self._cv:
            out = list(self._q)
            self._q.clear()
        return out

    def recv_blocking(self, timeout=None):
        """握手等场景使用的阻塞接收。"""
        with self._cv:
            if not self._q:
                self._cv.wait_for(lambda: self._q or self.peer_closed, timeout=timeout)
            if self._q:
                return self._q.popleft()
        raise TimeoutError("等待对端消息超时")

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass
