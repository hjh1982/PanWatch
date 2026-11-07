"""自动 AI 总开关:默认关闭,只认显式开启值。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.platform.ai import auto_policy
from src.platform.ai.auto_policy import is_ai_auto_enabled
from src.platform.persistence.database import Base
from src.platform.persistence.models import AppSettings


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_defaults_to_off_when_setting_missing(monkeypatch):
    monkeypatch.setattr(auto_policy, "env_default", lambda: False)
    engine, session = _session()
    try:
        assert is_ai_auto_enabled(session) is False
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("1", True), ("on", True), ("TRUE", True),
    ("false", False), ("0", False), ("", False), ("nonsense", False),
])
def test_reads_explicit_setting_value(monkeypatch, value, expected):
    monkeypatch.setattr(auto_policy, "env_default", lambda: False)
    engine, session = _session()
    try:
        session.add(AppSettings(key="ai_auto_enabled", value=value, description=""))
        session.commit()
        assert is_ai_auto_enabled(session) is expected
    finally:
        session.close()
        engine.dispose()
