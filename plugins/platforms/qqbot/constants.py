"""QQBot package-level constants shared across adapter, onboard, and other modules."""

from __future__ import annotations

import os

QQBOT_VERSION = "1.1.0"  # bump on functional changes to the adapter package
# Portal domain is overridable (QQ_PORTAL_HOST) for corporate proxies / test environments.
PORTAL_HOST = os.getenv("QQ_PORTAL_HOST", "q.qq.com")

API_BASE = "https://api.bot.qq.com"  # [tamako-local] 2026-10-28：官方 08-10 起把 REST 域名统一到 api.bot.qq.com；实测 /gateway 与 C2C 发消息均 200（WS 地址仍由服务端下发为 wss://api.sgroup.qq.com/websocket）
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_PATH = "/gateway"
ONBOARD_CREATE_PATH = "/lite/create_bind_task"
ONBOARD_POLL_PATH = "/lite/poll_bind_result"
QR_URL_TEMPLATE = "https://q.qq.com/qqbot/openclaw/connect.html?task_id={task_id}&_wv=2&source=hermes"

DEFAULT_API_TIMEOUT = 30.0
FILE_UPLOAD_TIMEOUT = 120.0
CONNECT_TIMEOUT_SECONDS = 20.0
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100
RATE_LIMIT_DELAY = 60  # seconds
QUICK_DISCONNECT_THRESHOLD = 5.0  # seconds
MAX_QUICK_DISCONNECT_COUNT = 3
ONBOARD_POLL_INTERVAL = 2.0  # seconds between poll_bind_result calls
ONBOARD_API_TIMEOUT = 10.0

MAX_MESSAGE_LENGTH = 4000
DEDUP_WINDOW_SECONDS = 3600  # [tamako-local] 300 太短：QQ WS 重连会重放旧事件（间隔 28~40 分钟），5 分钟窗口挡不住重复投递
DEDUP_MAX_SIZE = 5000

# QQ Bot message types / file media types
MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
MSG_TYPE_MEDIA = 7
MSG_TYPE_INPUT_NOTIFY = 6
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_VOICE = 3
MEDIA_TYPE_FILE = 4
