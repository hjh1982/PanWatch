"""自动 AI 行为总开关。

配置了模型不等于希望后台自动消耗 token：定时 Agent、首页策展、
会话自动命名、上下文自动压缩都受此开关控制；手动触发的 AI 功能不受影响。
默认关闭，读取失败也按关闭处理。
"""

from sqlalchemy.orm import Session

from src.platform.runtime.config import Settings

SETTING_KEY = "ai_auto_enabled"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def env_default() -> bool:
    return bool(Settings().ai_auto_enabled)


def _coerce(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def _read(db: Session) -> bool:
    from src.platform.persistence.models import AppSettings

    row = db.query(AppSettings).filter(AppSettings.key == SETTING_KEY).first()
    if row is None:
        return env_default()
    return _coerce(row.value)


def is_ai_auto_enabled(db: Session | None = None) -> bool:
    """全局自动 AI 开关。未显式开启时一律返回 False。"""
    try:
        if db is not None:
            return _read(db)
        from src.platform.persistence.database import SessionLocal

        with SessionLocal() as session:
            return _read(session)
    except Exception:
        return env_default()
