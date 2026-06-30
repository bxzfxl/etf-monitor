# -*- coding: utf-8 -*-
"""
Hermes 通知发送器
通过 Hermes WebUI 的 /webhook 端点发送消息，
由 Hermes 转发到已连接的 Telegram / WeChat 通道。

与现有 sender 的区别：
- 不需要独立的 Telegram Bot Token 或 微信 Webhook
- 所有通道管理集中在 Hermes，daily_stock_analysis 只负责生产消息
- 新增通道（Discord/Slack/邮件等）在 Hermes 侧配置，此处无需改动
"""

import logging
import time
from typing import Optional

import requests

from src.config import Config

logger = logging.getLogger(__name__)


class HermesSender:
    """
    Hermes 网关通知发送器

    配置项（.env）：
        HERMES_WEBHOOK_URL  — Hermes WebUI 的 /webhook 端点
        HERMES_API_TOKEN    — Hermes API Token（可在 WebUI 设置页生成）
        HERMES_NOTIFY_CHANNEL — 默认推送通道（telegram / wechat 等）
    """

    def __init__(self, config: Config):
        self.webhook_url = (
            getattr(config, "hermes_webhook_url", None)
            or ""
        ).rstrip("/")
        self.api_token = getattr(config, "hermes_api_token", None)
        self.default_channel = getattr(
            config, "hermes_notify_channel", "telegram"
        )
        self._max_retries = 3

    def _is_configured(self) -> bool:
        return bool(self.webhook_url)

    def send(
        self,
        content: str,
        *,
        channel: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        推送消息到 Hermes 网关

        Args:
            content: 消息内容（Markdown 格式，Hermes 会按目标平台渲染）
            channel: 推送通道（telegram / wechat 等），默认使用 HERMES_NOTIFY_CHANNEL
            timeout_seconds: 请求超时

        Returns:
            是否发送成功
        """
        if not self._is_configured():
            logger.warning("Hermes webhook 未配置（HERMES_WEBHOOK_URL），跳过推送")
            return False

        target_channel = channel or self.default_channel
        endpoint = f"{self.webhook_url}"

        payload = {
            "event": "notification",
            "text": content,
            "channel": target_channel,
        }

        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        timeout = timeout_seconds or 15

        for attempt in range(1, self._max_retries + 1):
            try:
                response = requests.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < self._max_retries:
                    delay = 2 ** attempt
                    logger.warning(
                        f"Hermes webhook 请求失败 (attempt {attempt}/{self._max_retries}): {e}, "
                        f"等待 {delay}s 重试..."
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"Hermes webhook 请求最终失败: {e}")
                return False

            if response.status_code in (200, 201, 202, 204):
                logger.info(f"Hermes 消息发送成功 → {target_channel}")
                return True
            elif 500 <= response.status_code < 600:
                if attempt < self._max_retries:
                    delay = 2 ** attempt
                    logger.warning(
                        f"Hermes 服务端错误 HTTP {response.status_code} "
                        f"(attempt {attempt}/{self._max_retries}), 等待 {delay}s 重试..."
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"Hermes 服务端错误 HTTP {response.status_code}: {response.text[:200]}")
                return False
            else:
                logger.error(f"Hermes webhook 返回 HTTP {response.status_code}: {response.text[:200]}")
                return False

        return False
