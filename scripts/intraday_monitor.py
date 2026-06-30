#!/usr/bin/python3
"""
盘中监控脚本 — Hermes Cron 集成版

由 Hermes cron 每 5 分钟调用（仅交易时段）。
stdout 投递到 Telegram。

配置：通过环境变量或硬编码默认值
  MONITOR_SYMBOL    标的代码（默认 560780）
  MONITOR_COST      成本价（默认 1.206）
  MONITOR_SHARES    底仓股数（默认 10000）
  MONITOR_T_FUND    做T资金（默认 10000）
  MONITOR_STOP      止损线（默认 1.20）
  MONITOR_TRAIL     止盈线（默认 1.30）
"""

import os
import sys
from datetime import datetime, time
from typing import Optional

# 硬编码默认值（可被环境变量覆盖）
SYMBOL = os.getenv("MONITOR_SYMBOL", "560780")
COST_PRICE = float(os.getenv("MONITOR_COST", "1.206"))
BASE_SHARES = int(os.getenv("MONITOR_SHARES", "10000"))
T_FUND = int(os.getenv("MONITOR_T_FUND", "10000"))
STOP_LOSS = float(os.getenv("MONITOR_STOP", "1.20"))
TRAIL_STOP = float(os.getenv("MONITOR_TRAIL", "1.30"))

FORCE = "--force" in sys.argv

TIME_WINDOWS = {
    "observe": (time(9, 30), time(10, 0)),
    "morning":  (time(10, 0), time(11, 30)),
    "afternoon": (time(13, 0), time(14, 30)),
    "check":    (time(14, 50), time(15, 0)),
}

WINDOW_LABELS = {
    "observe":   "🔍 观察期 — 只看不动",
    "morning":   "🟢 上午操作窗口",
    "afternoon": "🟢 下午操作窗口",
    "check":     "⚠️ 收盘前仓位检查！底仓必须复原",
}


def get_time_window() -> str:
    t = datetime.now().time()
    for name, (start, end) in TIME_WINDOWS.items():
        if start <= t < end:
            return name
    mo, mc = time(9, 30), time(15, 0)
    if mo <= t <= mc:
        if t < time(10, 0):   return "observe"
        if t < time(11, 30):  return "morning"
        if t < time(13, 0):   return "closed"
        if t < time(14, 50):  return "afternoon"
        return "check"
    return "closed"


def fetch_realtime() -> Optional[dict]:
    """新浪财经秒级行情（无需 AKShare 依赖）"""
    try:
        import requests, re
        code = f"sh{SYMBOL}" if SYMBOL.startswith(("5","6")) else f"sz{SYMBOL}"
        resp = requests.get(
            f"http://hq.sinajs.cn/list={code}",
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
        }
    except Exception:
        return None


def main():
    now = datetime.now()
    if not FORCE and now.weekday() >= 5:
        return
    window = get_time_window()
    if not FORCE and window == "closed":
        return

    data = fetch_realtime()
    if data is None:
        print(f"⚠️ 无法获取 {SYMBOL} 实时行情")
        return

    pnl_pct = (data["price"] - COST_PRICE) / COST_PRICE * 100

    lines = [
        f"📊 {SYMBOL} 盘中",
        "━━━━━━━━━━━━━━━",
        f"现价 {data['price']:.4f}  {data['change_pct']:+.2f}%",
        f"振幅 {data['amplitude']:.2f}%",
        f"底仓 {BASE_SHARES}股 浮盈 {pnl_pct:+.2f}%",
        f"止盈 {TRAIL_STOP}  止损 {STOP_LOSS}",
        "",
        WINDOW_LABELS.get(window, window),
    ]

    if data["price"] <= STOP_LOSS:
        lines.append("🚨🚨🚨 止损触发！")
    elif data["price"] <= TRAIL_STOP:
        lines.append("⚠️ 止盈线触发，建议减仓")

    if data["amplitude"] < 1.5 and window in ("morning", "afternoon"):
        lines.append("💤 振幅过小，不建议做T")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
