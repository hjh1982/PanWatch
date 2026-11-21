"""持仓账户 HTTP 用例的持久化边界回归测试。"""

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.platform.persistence.database import Base
from src.platform.persistence.models import Account, AccountTransaction, Position, Stock  # noqa: F401 - 注册关系模型


def _make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _add_txn(session, account, stock, side, quantity, price, fee=0.0, realized_pnl=None):
    txn = AccountTransaction(
        account_id=account.id,
        stock_id=stock.id,
        side=side,
        quantity=quantity,
        price=price,
        amount=price * quantity,
        fee=fee,
        realized_pnl=realized_pnl,
    )
    session.add(txn)
    session.commit()
    return txn


def test_delete_position_logs_relationship_names_before_commit():
    """删除持仓后仍能完成响应，日志不能访问已脱离会话的关系对象。"""
    from src.modules.portfolio.api.accounts import delete_position

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    account = Account(name="测试账户")
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    session.add_all([account, stock])
    session.flush()
    position = Position(
        account_id=account.id,
        stock_id=stock.id,
        cost_price=1500,
        quantity=100,
    )
    session.add(position)
    session.commit()

    result = delete_position(position.id, session)

    assert result == {"success": True}
    assert session.get(Position, position.id) is None
    session.close()
    engine.dispose()


def test_delete_position_does_not_read_detached_relationships_after_delete():
    """关系对象在删除提交后不可用时，删除接口仍应正常返回。"""
    from src.modules.portfolio.api.accounts import delete_position

    class Relation:
        def __init__(self, name: str, owner: "FakePosition"):
            self.name = name
            self._owner = owner

        def __getattribute__(self, attribute: str):
            if attribute == "name" and object.__getattribute__(self, "_owner").detached:
                raise AssertionError("删除提交后不应再访问懒加载关系")
            return object.__getattribute__(self, attribute)

    class FakePosition:
        id = 7

        def __init__(self):
            self.detached = False
            self.account = Relation("测试账户", self)
            self.stock = Relation("贵州茅台", self)

    class FakeQuery:
        def __init__(self, position):
            self.position = position

        def filter(self, _condition):
            return self

        def first(self):
            return self.position

    class FakeSession:
        def __init__(self, position):
            self.position = position

        def query(self, _model):
            return FakeQuery(self.position)

        def delete(self, position):
            position.detached = True

        def commit(self):
            return None

    position = FakePosition()

    assert delete_position(position.id, FakeSession(position)) == {"success": True}


def test_delete_buy_transaction_rolls_back_position_and_funds():
    """删除买入流水后，持仓数量/成本按加权平均剔除，现金加回金额和手续费。"""
    from src.modules.portfolio.api.accounts import delete_account_transaction

    engine, session = _make_session()
    account = Account(name="测试账户", available_funds=98995)
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    session.add_all([account, stock])
    session.flush()
    position = Position(account_id=account.id, stock_id=stock.id, cost_price=10.05, quantity=200)
    session.add(position)
    session.commit()
    txn = _add_txn(session, account, stock, "buy", 100, 10.0, fee=5.0)

    assert delete_account_transaction(account.id, txn.id, session) == {"success": True}
    session.refresh(position)
    assert position.quantity == 100
    assert position.cost_price == pytest.approx(10.05)
    assert account.available_funds == pytest.approx(100000)
    session.close()
    engine.dispose()


def test_delete_buy_transaction_removes_empty_position():
    """买回滚后持仓归零时删除持仓。"""
    from src.modules.portfolio.api.accounts import delete_account_transaction

    engine, session = _make_session()
    account = Account(name="测试账户", available_funds=98995)
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    session.add_all([account, stock])
    session.flush()
    position = Position(account_id=account.id, stock_id=stock.id, cost_price=10.05, quantity=100)
    session.add(position)
    session.commit()
    txn = _add_txn(session, account, stock, "buy", 100, 10.0, fee=5.0)

    assert delete_account_transaction(account.id, txn.id, session) == {"success": True}
    assert session.get(Position, position.id) is None
    assert account.available_funds == pytest.approx(100000)
    session.close()
    engine.dispose()


def test_delete_sell_transaction_restores_position_and_funds():
    """删除卖出流水后，持仓数量恢复、成本按卖出时均价还原，现金扣回净收入。"""
    from src.modules.portfolio.api.accounts import delete_account_transaction

    engine, session = _make_session()
    account = Account(name="测试账户", available_funds=1000)
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    session.add_all([account, stock])
    session.flush()
    position = Position(account_id=account.id, stock_id=stock.id, cost_price=10.0, quantity=100)
    session.add(position)
    session.commit()
    txn = _add_txn(session, account, stock, "sell", 40, 12.0, fee=5.0, realized_pnl=75.0)

    assert delete_account_transaction(account.id, txn.id, session) == {"success": True}
    session.refresh(position)
    assert position.quantity == 140
    assert position.cost_price == pytest.approx(10.0)
    assert account.available_funds == pytest.approx(525)
    session.close()
    engine.dispose()


def test_delete_closing_sell_transaction_recreates_position():
    """卖出清仓后持仓已删除时，删除该笔卖出应还原持仓。"""
    from src.modules.portfolio.api.accounts import delete_account_transaction

    engine, session = _make_session()
    account = Account(name="测试账户", available_funds=1000)
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    session.add_all([account, stock])
    session.commit()
    txn = _add_txn(session, account, stock, "sell", 40, 12.0, fee=5.0, realized_pnl=75.0)

    assert delete_account_transaction(account.id, txn.id, session) == {"success": True}
    position = session.query(Position).filter_by(account_id=account.id, stock_id=stock.id).one()
    assert position.quantity == 40
    assert position.cost_price == pytest.approx(10.0)
    assert account.available_funds == pytest.approx(525)
    session.close()
    engine.dispose()


def test_delete_buy_transaction_rejects_negative_position():
    """买入数量超过当前持仓（已被卖出）时拒绝回滚。"""
    from src.modules.portfolio.api.accounts import delete_account_transaction

    engine, session = _make_session()
    account = Account(name="测试账户", available_funds=1000)
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    session.add_all([account, stock])
    session.flush()
    session.add(Position(account_id=account.id, stock_id=stock.id, cost_price=10.0, quantity=50))
    session.commit()
    txn = _add_txn(session, account, stock, "buy", 100, 10.0)

    with pytest.raises(HTTPException) as exc:
        delete_account_transaction(account.id, txn.id, session)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "transaction_rollback_failed"
    assert session.get(AccountTransaction, txn.id) is not None
    session.close()
    engine.dispose()
