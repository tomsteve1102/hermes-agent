import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner(config: GatewayConfig) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


@pytest.mark.asyncio
async def test_preprocess_includes_slack_author_mention_for_shared_thread():
    """Shared Slack threads expose the current author's verifiable user ID
    next to the display name so 'mention me again' requests can bind the
    mention to the CURRENT speaker (#17916)."""
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.SLACK: PlatformConfig(enabled=True, token="fake"),
            },
        )
    )
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_name="team-channel",
        chat_type="group",
        user_id="U123",
        user_name="Alice",
        thread_id="171.000",
    )
    event = MessageEvent(text="mention me again", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "[Alice | Slack user <@U123>] mention me again"


@pytest.mark.asyncio
async def test_preprocess_appends_short_openid_for_shared_qq_group():
    """QQ 群共用一个会话时，昵称会重名也会被随手改；前缀里带上 openid 前 8 位，
    才能把一条群消息直接对齐到认人表里的成员（[tamako-local] 2026-11-03）。"""
    runner = _make_runner(
        GatewayConfig(
            platforms={Platform.QQBOT: PlatformConfig(enabled=True)},
            group_sessions_per_user=False,
        )
    )
    source = SessionSource(
        platform=Platform.QQBOT,
        chat_id="BF653803A09671D398ADC0DE9F0887B8",
        chat_name="Tom的猫窝",
        chat_type="group",
        user_id="404685B3A8CE33AEA2E779DC70C72C84",
        user_name="抹茶",
    )
    event = MessageEvent(text="在吗", source=source)

    result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result == "[抹茶#404685B3] 在吗"


