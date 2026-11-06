import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from src.modules.market import statistics_store
from src.modules.market.statistics_store import StatisticsStore, merge_mappings
from src.platform.persistence.models import StatisticSnapshot


def test_过期仅影响刷新不删除数据库(monkeypatch, statistics_db):
    monkeypatch.setattr(statistics_store.time, "time", lambda: 1000.0)
    store = StatisticsStore("test", 300)
    store.set("rows", {"2026-11-05": 12.3})
    monkeypatch.setattr(statistics_store.time, "time", lambda: 1301.0)
    fresh_instance = StatisticsStore("test", 300)
    assert fresh_instance.get("rows") is None
    assert fresh_instance.get("rows", allow_stale=True) == {"2026-11-05": 12.3}
    with statistics_db() as db:
        row = db.get(StatisticSnapshot, ("test", "rows"))
        assert row.fetched_at == 1000.0
        assert row.expires_at == 1300.0


def test_数据库跨进程读取(statistics_db):
    value = {"2026-11-05": {"price": 100.5, "missing": None}}
    StatisticsStore("restart", 300).set("rows", value)
    url = str(statistics_db.kw["bind"].url)
    result = subprocess.run(
        [sys.executable, "-c", """
import json, sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.modules.market import statistics_store
statistics_store.SessionLocal = sessionmaker(bind=create_engine(sys.argv[1]))
print(json.dumps(statistics_store.StatisticsStore('restart', 300).get('rows')))
""", url],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == value


def test_命名空间隔离和同键原子更新(statistics_db):
    a, b = StatisticsStore("a", 300), StatisticsStore("b", 300)
    a.set("rows", {"value": 1})
    b.set("rows", {"value": 2})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: a.set("rows", {"value": n}), range(8)))
    assert b.get("rows") == {"value": 2}
    assert a.get("rows")["value"] in range(8)
    with statistics_db() as db:
        assert db.scalar(select(func.count()).select_from(StatisticSnapshot)) == 2


@pytest.mark.parametrize("invalid", [float('nan'), float('inf'), object()])
def test_不写入非法JSON或非有限数值(invalid):
    store = StatisticsStore("valid", 300)
    store.set("rows", {"value": 1})
    with pytest.raises((ValueError, TypeError)):
        store.set("rows", {"value": invalid})
    assert store.get("rows") == {"value": 1}


def test_保留旧日期及字段并接受同日修订():
    previous = {"2026-10": {"a": 1, "b": 2}, "2026-09": {"a": 0}}
    incoming = {"2026-10": {"a": 3, "b": None}, "2026-11": {"a": 4}}
    merged = merge_mappings(previous, incoming)
    assert merged == {"2026-09": {"a": 0}, "2026-10": {"a": 3, "b": 2}, "2026-11": {"a": 4}}
    assert previous["2026-10"]["a"] == 1
    assert incoming["2026-10"]["b"] is None
