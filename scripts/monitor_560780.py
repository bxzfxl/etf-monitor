#!/usr/bin/python3
"""560780 ETF 盘中监控 - Hermes Cron 脚本版

由 Hermes cron 每 5 分钟调用（仅交易时段），
stdout 会通过 Hermes 投递到微信/Telegram。
"""

import sys
from datetime import datetime, time
from typing import Optional

FORCE = "--force" in sys.argv

# ----- 配置 -----
SYMBOL = "560780"
COST_PRICE = 1.206
BASE_SHARES = 10000
T_FUND = 10000
STOP_LOSS = 1.20
TRAIL_STOP = 1.30

TIME_WINDOWS = {
    "observe": (time(9, 30), time(10, 0)),
    "morning": (time(10, 0), time(11, 30)),
    "afternoon": (time(13, 0), time(14, 30)),
    "check": (time(14, 50), time(15, 0)),
}

WINDOW_LABELS = {
    "observe": "🔍 观察期 — 只看不动",
    "morning": "🟢 上午操作窗口",
    "afternoon": "🟢 下午操作窗口",
    "check": "⚠️ 收盘前仓位检查！底仓 10000 股必须复原",
}


def get_time_window() -> str:
    t = datetime.now().time()
    for name, (start, end) in TIME_WINDOWS.items():
        if start <= t < end:
            return name
    market_open = time(9, 30)
    market_close = time(15, 0)
    if market_open <= t <= market_close:
        if t < time(10, 0):
            return "observe"
        if t < time(11, 30):
            return "morning"
        if t < time(13, 0):
            return "closed"
        if t < time(14, 50):
            return "afternoon"
        return "check"
    return "closed"


def fetch_realtime() -> Optional[dict]:
    """从新浪财经获取 560780 实时行情（秒级响应）"""
    try:
        import requests, re
        resp = requests.get(
            "http://hq.sinajs.cn/list=sh560780",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=5,
        )
        m = re.search(r'=\"(.+)\"', resp.text)
        if not m:
            return None
        f = m.group(1).split(",")
        price = float(f[3])
        pre = float(f[2])
        high = float(f[4])
        low = float(f[5])
        return {
            "price": price,
            "open": float(f[1]),
            "high": high,
            "low": low,
            "pre_close": pre,
            "change_pct": (price - pre) / pre * 100,
            "amplitude": (high - low) / pre * 100 if pre > 0 else 0,
            "volume": float(f[8]) if len(f) > 8 else 0,
        }
    except Exception:
        return None


def main():
    now = datetime.now()
    if not FORCE and now.weekday() >= 5:
        return

    window = get_time_window()
    if not FORCE and window == "closed":
        return  # 非交易时段静默

    data = fetch_realtime()
    if data is None:
        print(f"⚠️ 无法获取 560780 实时行情")
        return

    pnl_pct = (data["price"] - COST_PRICE) / COST_PRICE * 100

    # 格式化成微信可读的消息
    lines = []
    lines.append(f"📊 560780 盘中")
    lines.append(f"━━━━━━━━━━━━━━━")
    lines.append(f"现价 {data['price']:.4f}  {data['change_pct']:+.2f}%")
    lines.append(f"振幅 {data['amplitude']:.2f}%")
    lines.append(f"底仓 {BASE_SHARES}股 浮盈 {pnl_pct:+.2f}%")
    lines.append(f"止盈 {TRAIL_STOP}  止损 {STOP_LOSS}")
    lines.append("")

    label = WINDOW_LABELS.get(window, window)
    lines.append(label)

    # 告警
    if data["price"] <= STOP_LOSS:
        lines.append("🚨🚨🚨 止损触发！")
    elif data["price"] <= TRAIL_STOP:
        lines.append("⚠️ 止盈线触发，建议减仓")

    if data["amplitude"] < 1.5 and window in ("morning", "afternoon"):
        lines.append("💤 振幅过小，不建议做T")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
