# -*- coding: utf-8 -*-
"""
560780 盘中监控模块

交易时段每 5 分钟轮询一次：
1. 从 AKShare 拉取 560780 实时行情
2. 计算 RSI / MA 乖离 / 日内振幅等指标
3. 根据时间窗策略生成操作信号
4. 通过 Hermes sender 推送到 Telegram

集成方式：
- 由 main.py 的调度器调用（SCHEDULE_TIMES 或独立定时器）
- 或通过 Hermes cron 外部触发（脚本模式）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ===== 仓位配置（硬编码，后续可迁移到 .env） =====
SYMBOL = "560780"
COST_PRICE = 1.206
BASE_SHARES = 10000
T_FUND = 10000
T_MAX_SHARES = 7000
STOP_LOSS = 1.20
TRAIL_STOP = 1.30
NO_TRADE_AMPLITUDE = 1.5  # 振幅不足不做 T (%)
MA_PERIOD = 10
RSI_PERIOD = 14
RSI_OVERBOUGHT = 85
RSI_OVERSOLD = 25
MA_GAP_ALERT = 15  # 乖离率预警 (%)
T_MAX_LOSS_PCT = 2.0  # 做 T 单日最大亏损 (%)


# ===== 时间窗定义 =====
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
    "check": "⚠️ 收盘前仓位检查 — 确保底仓 10000 股复原",
    "closed": "⚫ 非交易时段",
}


@dataclass
class RealtimeData:
    """实时行情数据"""
    price: float
    open: float
    high: float
    low: float
    pre_close: float
    volume: float
    change_pct: float
    amplitude: float = 0.0  # 日内振幅（计算得出）


@dataclass
class TechIndicators:
    """技术指标"""
    rsi: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma_gap_pct: Optional[float] = None  # 价格 vs MA10 乖离率
    trend: str = "unknown"  # up / down / sideways


@dataclass
class StrategySignal:
    """策略信号"""
    window: str = "closed"             # 当前时间窗
    pnl_pct: float = 0.0               # 浮盈 %
    can_trade: bool = False            # 是否可做 T
    direction: str = "none"            # 做 T 方向: long / short / none
    reason: str = ""                   # 决策理由
    stop_triggered: bool = False       # 止损触发
    trail_triggered: bool = False      # 止盈触发
    alerts: List[str] = field(default_factory=list)  # 告警列表


def is_trading_day() -> bool:
    """判断是否是交易日（简化：周一到周五）"""
    return datetime.now().weekday() < 5


def get_time_window() -> str:
    """返回当前所处的时间窗"""
    t = datetime.now().time()
    for name, (start, end) in TIME_WINDOWS.items():
        if start <= t < end:
            return name
    # 判断是否在盘中但不在窗口内（如 11:30-13:00 午休）
    market_open = time(9, 30)
    market_close = time(15, 0)
    if market_open <= t <= market_close:
        if t < time(10, 0):
            return "observe"
        if t < time(11, 30):
            return "morning"
        if t < time(13, 0):
            return "closed"  # 午休
        if t < time(14, 50):
            return "afternoon"
        return "check"
    return "closed"


def fetch_realtime() -> Optional[RealtimeData]:
    """从 AKShare 获取 560780 实时行情"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        row = df[df["代码"] == SYMBOL]
        if row.empty:
            logger.warning(f"AKShare 未返回 {SYMBOL} 的实时行情")
            return None
        r = row.iloc[0]
        price = float(r["最新价"])
        high = float(r["最高"])
        low = float(r["最低"])
        pre_close = float(r["昨收"])
        amplitude = (high - low) / pre_close * 100 if pre_close > 0 else 0
        return RealtimeData(
            price=price,
            open=float(r["今开"]),
            high=high,
            low=low,
            pre_close=pre_close,
            volume=float(r.get("成交量", 0)),
            change_pct=float(r.get("涨跌幅", 0)),
            amplitude=amplitude,
        )
    except Exception as e:
        logger.error(f"获取实时行情失败: {e}")
        return None


def fetch_history(days: int = 30) -> List[float]:
    """获取 560780 历史收盘价列表"""
    try:
        import akshare as ak
        # 使用 ETF 历史行情接口
        df = ak.fund_etf_hist_em(
            symbol=SYMBOL,
            period="daily",
            start_date=(datetime.now().replace(day=1)).strftime("%Y%m%d")
            if days <= 22
            else (datetime.now().replace(day=1, month=datetime.now().month - 1)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
            adjust="",
        )
        if df.empty:
            # 回退：用 stock 接口
            df = ak.stock_zh_a_hist(
                symbol=SYMBOL,
                period="daily",
                adjust="qfq",
            )
        prices = df["收盘"].astype(float).tolist()
        return prices[-days:] if len(prices) > days else prices
    except Exception as e:
        logger.error(f"获取历史行情失败: {e}")
        return []


def calc_rsi(prices: List[float], period: int = RSI_PERIOD) -> Optional[float]:
    """计算 RSI"""
    if len(prices) < period + 1:
        return None
    close_prices = np.array(prices[-period - 1:])
    deltas = np.diff(close_prices)
    gains = np.maximum(deltas, 0)
    losses = np.abs(np.minimum(deltas, 0))

    # 使用 Wilder's smoothing
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if len(gains) > period:
        avg_gain = (avg_gain * (period - 1) + gains[-1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[-1]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_ma(prices: List[float], period: int) -> Optional[float]:
    """计算移动平均"""
    if len(prices) < period:
        return None
    return float(np.mean(prices[-period:]))


def compute_indicators(data: RealtimeData) -> Tuple[TechIndicators, List[str]]:
    """计算技术指标"""
    alerts: List[str] = []
    history = fetch_history(30)

    indicators = TechIndicators()

    if history:
        indicators.rsi = calc_rsi(history, RSI_PERIOD)
        indicators.ma10 = calc_ma(history, MA_PERIOD)
        indicators.ma20 = calc_ma(history, 20)

        # 均线趋势
        if indicators.ma10 and indicators.ma20:
            if indicators.ma10 > indicators.ma20:
                indicators.trend = "up"
            else:
                indicators.trend = "down"

        # 乖离率
        if indicators.ma10 and indicators.ma10 > 0:
            indicators.ma_gap_pct = (
                (data.price - indicators.ma10) / indicators.ma10 * 100
            )

    # 告警判断
    if indicators.rsi is not None:
        if indicators.rsi > RSI_OVERBOUGHT:
            alerts.append(f"🔥 RSI 极度超买 ({indicators.rsi:.0f})，警惕回调")
        elif indicators.rsi < RSI_OVERSOLD:
            alerts.append(f"🧊 RSI 极度超卖 ({indicators.rsi:.0f})，等企稳")

    if indicators.ma_gap_pct is not None:
        if indicators.ma_gap_pct > MA_GAP_ALERT:
            alerts.append(f"📈 价格远离 10MA +{indicators.ma_gap_pct:.1f}%，回调概率大")
        elif indicators.ma_gap_pct < -MA_GAP_ALERT:
            alerts.append(f"📉 价格低于 10MA {indicators.ma_gap_pct:.1f}%，超跌")

    return indicators, alerts


def evaluate(data: RealtimeData, indicators: TechIndicators) -> StrategySignal:
    """评估策略信号"""
    window = get_time_window()
    pnl_pct = (data.price - COST_PRICE) / COST_PRICE * 100

    signal = StrategySignal(
        window=window,
        pnl_pct=pnl_pct,
    )

    # 止损/止盈
    if data.price <= STOP_LOSS:
        signal.stop_triggered = True
        signal.alerts.append("🚨 硬止损触发！价格已跌破 1.20")
    elif data.price <= TRAIL_STOP:
        signal.trail_triggered = True
        signal.alerts.append("⚠️ 止盈线触发 (1.30)，建议减仓 3000 股")

    # 振幅不足不做 T
    if data.amplitude < NO_TRADE_AMPLITUDE:
        signal.can_trade = False
        signal.reason = f"振幅过小 ({data.amplitude:.2f}%)，不做 T"
        return signal

    # 非操作窗口不做 T
    if window not in ("morning", "afternoon"):
        signal.can_trade = False
        signal.reason = WINDOW_LABELS.get(window, window)
        return signal

    # 判断做 T 方向
    signal.can_trade = True

    if data.change_pct > 2:
        signal.direction = "short"
        signal.reason = f"高开 +{data.change_pct:.2f}%，关注反 T（先卖后买）"
    elif data.change_pct < -2:
        signal.direction = "long"
        signal.reason = f"大幅低开 {data.change_pct:.2f}%，等企稳后正 T（先买后卖）"
    elif indicators.rsi and indicators.rsi > RSI_OVERBOUGHT:
        signal.direction = "short"
        signal.reason = f"RSI 超买，优先反 T"
    elif indicators.rsi and indicators.rsi < RSI_OVERSOLD:
        signal.direction = "long"
        signal.reason = f"RSI 超卖，关注正 T 机会"
    else:
        signal.reason = "趋势中性，等待明确信号"

    return signal


def format_signal(
    data: RealtimeData,
    indicators: TechIndicators,
    signal: StrategySignal,
) -> str:
    """格式化信号为 Telegram Markdown 消息"""
    lines = []
    lines.append(f"📊 *560780 盘中监控*")
    lines.append("")

    # 行情
    lines.append(f"💰 现价: *{data.price:.4f}*  |  {data.change_pct:+.2f}%")
    lines.append(
        f"📈 今开 {data.open:.4f} 高 {data.high:.4f} 低 {data.low:.4f}"
    )
    lines.append(f"📉 昨收 {data.pre_close:.4f}  |  振幅 {data.amplitude:.2f}%")

    # 仓位
    pnl_emoji = "🟢" if signal.pnl_pct > 0 else "🔴"
    lines.append(
        f"💼 底仓 {BASE_SHARES}股 成本 {COST_PRICE}  |  "
        f"{pnl_emoji} 浮盈 *{signal.pnl_pct:+.2f}%*"
    )

    # 技术面
    if indicators.rsi is not None:
        rsi_str = f"RSI *{indicators.rsi:.0f}*"
        if indicators.rsi > RSI_OVERBOUGHT:
            rsi_str += " 🔥超买"
        elif indicators.rsi < RSI_OVERSOLD:
            rsi_str += " 🧊超卖"
        lines.append(rsi_str)
    if indicators.ma_gap_pct is not None:
        lines.append(f"MA10乖离: *{indicators.ma_gap_pct:+.1f}%*")

    lines.append("")

    # 时间窗
    lines.append(WINDOW_LABELS.get(signal.window, signal.window))
    lines.append("")

    # 操作建议
    if signal.can_trade:
        direction_labels = {
            "long": "🟢 *正T* — 先买后卖",
            "short": "🔴 *反T* — 先卖后买",
            "none": "⏸️ 观望",
        }
        lines.append(f"🎯 建议: {direction_labels.get(signal.direction, '观望')}")
        t_shares = min(T_MAX_SHARES, int(T_FUND / data.price))
        lines.append(f"   可用 ¥{T_FUND}，最多 {t_shares} 股")
    else:
        lines.append(f"⏸️ {signal.reason}")

    # 告警
    if signal.stop_triggered:
        lines.append("")
        lines.append("🚨 *止损触发！立即处理*")
    elif signal.trail_triggered:
        lines.append("")
        lines.append("⚠️ *止盈线触发*")

    all_alerts = signal.alerts + []
    if all_alerts:
        lines.append("")
        for a in all_alerts:
            lines.append(a)

    return "\n".join(lines)


def run_tick(*, send_notification: bool = True) -> Optional[str]:
    """
    执行一次盘中轮询

    Args:
        send_notification: 是否发送通知

    Returns:
        格式化的信号文本，或 None（非交易时段）
    """
    if not is_trading_day():
        return None

    window = get_time_window()
    if window == "closed":
        return None

    data = fetch_realtime()
    if data is None:
        logger.error("无法获取行情，跳过本轮")
        return None

    indicators, _ = compute_indicators(data)
    signal = evaluate(data, indicators)
    text = format_signal(data, indicators, signal)

    if send_notification:
        try:
            from src.notification_sender.hermes_sender import HermesSender
            from src.config import get_config

            config = get_config()
            sender = HermesSender(config)
            if sender._is_configured():
                sender.send(text)
        except Exception as e:
            logger.error(f"发送通知失败: {e}")

    return text


# ===== 脚本入口（供 Hermes cron 直接调用） =====
if __name__ == "__main__":
    import sys

    result = run_tick(send_notification=False)
    if result:
        print(result)
    else:
        print(WINDOW_LABELS.get(get_time_window(), "⚫ 非交易时段"))
