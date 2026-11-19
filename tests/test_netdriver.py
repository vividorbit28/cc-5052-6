"""TCP 联机路径测试：两个 NetDriver 经真实套接字同步。"""

import socket
import threading
import time
import unittest

from lockstep.commands import make_focus, make_move
from lockstep.netdriver import NetDriver


def reserve_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def connect_retry(port, timeout=15):
    last = None
    for _ in range(100):
        try:
            return NetDriver.connect("127.0.0.1", port, timeout=timeout)
        except ConnectionRefusedError as e:
            last = e
            time.sleep(0.05)
    raise last


def drive(driver, script, ticks):
    """按真实时间驱动一端；script: (at_tick, cmd) 列表。"""
    script = sorted(script, key=lambda s: s[0])
    issued = 0
    deadline = time.monotonic() + 30
    while driver.ep.world.tick < ticks and not driver.halted:
        if time.monotonic() > deadline:
            raise TimeoutError("联机推进超时")
        t = driver.ep.world.tick
        while issued < len(script) and script[issued][0] <= t:
            driver.issue(driver.player, script[issued][1])
            issued += 1
        driver.update(0.02)
        time.sleep(0.002)


class TestTcpSync(unittest.TestCase):
    def test_two_endpoints_over_tcp(self):
        port = reserve_port()
        server = {}

        def run_server():
            server["d"] = NetDriver.listen(port, seed=77, timeout=15)

        t = threading.Thread(target=run_server)
        t.start()
        client = connect_retry(port)
        t.join(timeout=15)
        srv = server["d"]

        script_a = [(2, make_move(2, 10, 7)), (18, make_focus(14, 7))]
        script_b = [(3, make_move(8, 14, 7)), (20, make_focus(10, 8))]
        ta = threading.Thread(target=drive, args=(srv, script_a, 80))
        tb = threading.Thread(target=drive, args=(client, script_b, 80))
        ta.start(); tb.start()
        ta.join(); tb.join()

        # 等最后一帧的对端摘要送达（推进已停，只收信）
        for _ in range(200):
            if len(srv.checks()) >= 80 and len(client.checks()) >= 80:
                break
            srv.ep.pump()
            client.ep.pump()
            time.sleep(0.01)

        self.assertIsNone(srv.divergence())
        self.assertIsNone(client.divergence())
        checks = srv.checks()
        self.assertGreaterEqual(len(checks), 80)
        self.assertTrue(all(ok for _t, ok, _a, _b in checks))
        # 两端各自模拟出的世界逐帧一致
        self.assertEqual({t: d for t, d in srv.ep.digests.items()},
                         {t: d for t, d in client.ep.digests.items()})
        srv.link.close()
        client.link.close()

    def test_remote_rejects_foreign_command(self):
        port = reserve_port()
        server = {}

        def run_server():
            server["d"] = NetDriver.listen(port, seed=78, timeout=15)

        t = threading.Thread(target=run_server)
        t.start()
        client = connect_retry(port)
        t.join(timeout=15)
        srv = server["d"]
        ok, msg = srv.issue(1, make_move(8, 5, 5))  # 主机是指挥小队0的
        self.assertFalse(ok)
        self.assertIn("本端", msg)
        ok, msg = client.issue(1, make_move(8, 5, 5))
        self.assertTrue(ok)
        srv.link.close()
        client.link.close()


if __name__ == "__main__":
    unittest.main()
