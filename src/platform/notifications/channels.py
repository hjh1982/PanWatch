"""从数据库解析 Agent 的通知渠道（stock_agent 覆盖 → agent 绑定 → 系统默认）。"""

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import AgentConfig, NotifyChannel, StockAgent


def resolve_notify_channels(
    agent_name: str, stock_agent_id: int | None = None
) -> list[NotifyChannel]:
    db = SessionLocal()
    try:
        channel_ids = None

        if stock_agent_id:
            sa = db.query(StockAgent).filter(StockAgent.id == stock_agent_id).first()
            if sa and sa.notify_channel_ids:
                channel_ids = sa.notify_channel_ids

        if channel_ids is None:
            agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
            if agent and agent.notify_channel_ids:
                channel_ids = agent.notify_channel_ids

        if channel_ids:
            channels = (
                db.query(NotifyChannel)
                .filter(
                    NotifyChannel.id.in_(channel_ids),
                    NotifyChannel.enabled == True,
                )
                .all()
            )
        else:
            channels = (
                db.query(NotifyChannel)
                .filter(
                    NotifyChannel.is_default == True,
                    NotifyChannel.enabled == True,
                )
                .all()
            )

        for ch in channels:
            db.expunge(ch)
        return channels
    finally:
        db.close()


def resolve_bark_channel(agent_name: str, stock_agent_id: int | None = None) -> NotifyChannel | None:
    """取 Agent 可用渠道中的第一个 Bark 渠道。"""
    for channel in resolve_notify_channels(agent_name, stock_agent_id):
        if channel.type == "bark" and (channel.config or {}).get("device_key"):
            return channel
    return None
