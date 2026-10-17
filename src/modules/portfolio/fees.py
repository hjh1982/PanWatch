"""交易手续费按市场规则自动计算。

所有金额以该笔成交的币种计（A股/港股通人民币、港股港元），结果保留两位小数。
"""

# A股
CN_STAMP_TAX_RATE = 0.0005  # 印花税：仅卖出 0.05%
CN_TRANSFER_FEE_RATE = 0.00001  # 过户费：沪深双向 0.001%（万0.1）

# 港股
HK_STAMP_TAX_RATE = 0.001  # 印花税：双向 0.1%，不足 1 港元按 1 港元
HK_STAMP_TAX_MIN = 1.0
HK_SFC_LEVY_RATE = 0.000027  # 证监会交易征费 0.0027%
HK_FRC_LEVY_RATE = 0.0000015  # 会计及财务汇报局征费 0.00015%
HK_EXCHANGE_FEE_RATE = 0.00005  # 联交所交易费 0.005%
HK_CCASS_RATE = 0.00002  # 结算费 0.002%
HK_CCASS_MIN = 2.0
HK_CCASS_MAX = 100.0


def calc_fee(
    market: str,
    side: str,
    amount: float,
    commission_rate: float = 0.00025,
    commission_min: float = 5.0,
) -> dict:
    """计算单笔交易手续费。

    返回 {fee, commission, stamp_tax, transfer_fee, other}。
    """
    market = (market or "").upper()
    is_sell = side == "sell"
    commission = amount * commission_rate
    if commission_min and commission < commission_min:
        commission = commission_min
    stamp_tax = 0.0
    transfer_fee = 0.0
    other = 0.0

    if market == "CN":
        if is_sell:
            stamp_tax = round(amount * CN_STAMP_TAX_RATE, 2)
        transfer_fee = round(amount * CN_TRANSFER_FEE_RATE, 2)
    elif market == "HK":
        stamp_tax = max(amount * HK_STAMP_TAX_RATE, HK_STAMP_TAX_MIN)
        other = round(
            amount * (HK_SFC_LEVY_RATE + HK_FRC_LEVY_RATE + HK_EXCHANGE_FEE_RATE),
            2,
        )
        transfer_fee = min(
            max(round(amount * HK_CCASS_RATE, 2), HK_CCASS_MIN), HK_CCASS_MAX
        )
    else:
        # 其他市场：只收佣金
        pass

    commission = round(commission, 2)
    stamp_tax = round(stamp_tax, 2)
    transfer_fee = round(transfer_fee, 2)
    other = round(other, 2)
    return {
        "fee": round(commission + stamp_tax + transfer_fee + other, 2),
        "commission": commission,
        "stamp_tax": stamp_tax,
        "transfer_fee": transfer_fee,
        "other": other,
    }
