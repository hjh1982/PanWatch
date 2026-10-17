// 交易手续费按市场规则自动计算（前端预览，与后端 src/modules/portfolio/fees.py 保持一致）

const CN_STAMP_TAX = 0.0005
const CN_TRANSFER = 0.00001
const HK_STAMP = 0.001
const HK_STAMP_MIN = 1
const HK_OTHER = 0.000027 + 0.0000015 + 0.00005
const HK_CCASS = 0.00002
const HK_CCASS_MIN = 2
const HK_CCASS_MAX = 100

const r2 = (n: number) => Math.round(n * 100) / 100

export interface FeePreview {
  fee: number
  commission: number
  stamp_tax: number
  transfer_fee: number
  other: number
}

export function calcFeePreview(
  market: string,
  side: 'buy' | 'sell',
  amount: number,
  commissionRate = 0.00025,
  commissionMin = 5,
): FeePreview {
  const m = (market || '').toUpperCase()
  let commission = amount * commissionRate
  if (commissionMin && commission < commissionMin) commission = commissionMin
  let stamp = 0
  let transfer = 0
  let other = 0

  if (m === 'CN') {
    if (side === 'sell') stamp = r2(amount * CN_STAMP_TAX)
    transfer = r2(amount * CN_TRANSFER)
  } else if (m === 'HK') {
    stamp = Math.max(amount * HK_STAMP, HK_STAMP_MIN)
    other = r2(amount * HK_OTHER)
    transfer = Math.min(Math.max(r2(amount * HK_CCASS), HK_CCASS_MIN), HK_CCASS_MAX)
  }

  commission = r2(commission)
  stamp = r2(stamp)
  transfer = r2(transfer)
  other = r2(other)
  return {
    fee: r2(commission + stamp + transfer + other),
    commission,
    stamp_tax: stamp,
    transfer_fee: transfer,
    other,
  }
}
