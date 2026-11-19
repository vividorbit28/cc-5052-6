"""命令行入口。

窗口模式（默认）：
    python3 -m lockstep                          # 单窗口本机双端（进程内延迟链路）
    python3 -m lockstep --listen 7777            # 联机主机窗口（指挥小队0）
    python3 -m lockstep --connect 127.0.0.1:7777 # 联机客户端窗口（指挥小队1）

无头模式：
    python3 -m lockstep --selftest   # 固定脚本逐帧打印摘要（确定性自检）
    python3 -m lockstep --demo       # 验收演示：帧率/延迟一致性、回放、分歧定位
    python3 -m lockstep --replay rec.json
"""

import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m lockstep", description="策略锁步演练运行时")
    parser.add_argument("--listen", type=int, metavar="PORT",
                        help="作为主机监听端口（联机窗口，指挥小队0）")
    parser.add_argument("--connect", metavar="HOST:PORT",
                        help="连接主机（联机窗口，指挥小队1）")
    parser.add_argument("--selftest", action="store_true",
                        help="无头自检：固定脚本逐帧打印状态摘要")
    parser.add_argument("--demo", action="store_true",
                        help="无头验收演示")
    parser.add_argument("--replay", metavar="FILE",
                        help="回放校验录制的输入脚本")
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--latency", type=int, default=80,
                        help="本机双端的模拟链路延迟（毫秒）")
    parser.add_argument("--input-delay", type=int, default=2,
                        help="输入延迟（帧）")
    args = parser.parse_args(argv)

    if args.selftest:
        from .selftest import run
        return 0 if run(seed=args.seed) else 1
    if args.demo:
        from .demo import run
        return 0 if run() else 1
    if args.replay:
        from .replay import compare_configurations, load_recording, verify
        rec = load_recording(args.replay)
        ok, _t, msg = verify(rec)
        print(msg)
        ok2, msg2 = compare_configurations(
            rec, [(0, 1 / 60), (300, 1 / 20), (120, 1 / 30)])
        print(msg2)
        return 0 if ok and ok2 else 1

    try:
        from .ui import LockstepWindow  # 延迟导入：无头模式不依赖 tkinter
    except ModuleNotFoundError:
        print("未找到 tkinter，无法打开窗口。请安装 python3-tk，"
              "或改用无头模式：--selftest / --demo / --replay")
        return 2
    if args.listen:
        from .netdriver import NetDriver
        print(f"等待对端连接 0.0.0.0:{args.listen} ...")
        driver = NetDriver.listen(args.listen, seed=args.seed,
                                  input_delay=args.input_delay)
        print("对端已连接。")
    elif args.connect:
        from .netdriver import NetDriver
        host, _, port = args.connect.partition(":")
        driver = NetDriver.connect(host, int(port or "7777"))
    else:
        from .session import LockstepSession
        driver = LockstepSession(seed=args.seed, latency_ms=args.latency,
                                 input_delay=args.input_delay)
    LockstepWindow(driver).run()
    return 0
