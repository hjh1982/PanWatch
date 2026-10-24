import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchAPI } from '@panwatch/api/client'
import EtfSharesPage from '@/pages/EtfShares'

vi.mock('@panwatch/api/client', () => ({ fetchAPI: vi.fn() }))

const catalog = {
  default_group: 'hs300',
  default_days: 90,
  allowed_days: [30, 60, 90, 120],
  groups: [
    {
      id: 'hs300',
      label: '沪深300 ETF',
      category: 'broad',
      available: true,
      accent: '#e5484d',
      funds: [
        { code: '510300', name: '沪深300ETF华泰柏瑞' },
        { code: '510310', name: '沪深300ETF易方达' },
        { code: '510320', name: '沪深300ETF中金' },
        { code: '510330', name: '沪深300ETF华夏' },
        { code: '510350', name: '沪深300ETF工银' },
      ],
    },
    {
      id: 'zz500',
      label: '中证500 ETF',
      category: 'broad',
      available: true,
      accent: '#3b82f6',
      funds: [
        { code: '510500', name: '中证500ETF南方' },
        { code: '510510', name: '中证500ETF广发' },
        { code: '510580', name: '中证500ETF易方达' },
        { code: '512500', name: '中证500ETF华夏' },
        { code: '512510', name: '中证500ETF华泰柏瑞' },
      ],
    },
    {
      id: 'margin',
      label: '融资融券',
      category: 'broad',
      available: true,
      accent: '#0ea5a4',
      funds: [],
    },
    {
      id: 'coalPrice',
      label: '煤炭价格',
      category: 'commodity',
      available: true,
      accent: '#f97316',
      funds: [],
    },
    {
      id: 'aluminum',
      label: '电解铝·氧化铝',
      category: 'commodity',
      available: true,
      accent: '#f97316',
      funds: [],
    },
    {
      id: 'aluminumInventory',
      label: '铝锭库存',
      category: 'commodity',
      available: true,
      accent: '#f59e0b',
      funds: [],
    },
    {
      id: 'threeGorges',
      label: '三峡水库水位',
      category: 'commodity',
      available: true,
      accent: '#22c55e',
      funds: [],
    },
    {
      id: 'goldReserves',
      label: '黄金储备',
      category: 'commodity',
      available: false,
      accent: '#eab308',
      funds: [],
    },
  ],
}

const dates = ['2026-10-19', '2026-10-20', '2026-10-21']

function fund(code: string, name: string, latest: number, delta: number) {
  return {
    code,
    name,
    latest,
    latest_date: '2026-10-21',
    delta,
    delta_pct: (delta / (latest - delta)) * 100,
    values: [latest - delta * 2, latest - delta, latest],
  }
}

const hs300Curve = {
  group: 'hs300',
  group_label: '沪深300 ETF',
  as_of: '2026-10-21',
  dates,
  funds: [
    fund('510300', '沪深300ETF华泰柏瑞', 301.990877, 7.983),
    fund('510310', '沪深300ETF易方达', 170.62844, 0.342),
    fund('510320', '沪深300ETF中金', 6.520685, 0),
    fund('510330', '沪深300ETF华夏', 97.022498, 0.78),
    fund('510350', '沪深300ETF工银', 6.859147, 0),
  ],
  coverage: { trading_days: 87, target: 90, complete: true },
}

const zz500Curve = {
  group: 'zz500',
  group_label: '中证500 ETF',
  as_of: '2026-10-21',
  dates,
  funds: [fund('510500', '中证500ETF南方', 60.997686, -0.108)],
  coverage: { trading_days: 90, target: 90, complete: true },
}

const coalCurve = {
  group: 'coalPrice',
  group_label: '煤炭价格',
  scope: 'CCI5500',
  as_of: '2026-10-14',
  dates: [
    '2025-10-13',
    '2025-10-14',
    '2026-10-10',
    '2026-10-13',
    '2026-10-14',
  ],
  metrics: [
    {
      id: 'cci_5500',
      latest: 1018,
      latest_date: '2026-10-14',
      delta: 5,
      delta_pct: 0.4936,
    },
  ],
  series: {
    cci_5500: [972, 976, 1003, 1013, 1018],
  },
  coverage: { trading_days: 5, target: 5, complete: true },
  storage: {
    database: '/srv/panwatch/data/panwatch.db',
    table: 'coal_price_points',
    seed: '/srv/panwatch/src/modules/market/resources/coal_cci_5500.csv',
  },
}

const aluminumCurve = {
  group: 'aluminum',
  group_label: '电解铝·氧化铝',
  scope: 'A00铝 · 氧化铝(山西)',
  as_of: '2026-10-22',
  dates: ['2026-10-20', '2026-10-21', '2026-10-22'],
  metrics: [
    {
      id: 'a00_avg',
      latest: 23710,
      latest_date: '2026-10-22',
      delta: 10,
      delta_pct: 0.0422,
    },
    {
      id: 'alumina_avg',
      latest: 2730,
      latest_date: '2026-10-22',
      delta: -5,
      delta_pct: -0.1828,
    },
    {
      id: 'spread',
      latest: 20980,
      latest_date: '2026-10-22',
      delta: 15,
      delta_pct: 0.0716,
    },
  ],
  series: {
    a00_avg: [23680, 23700, 23710],
    a00_low: [23580, 23600, 23620],
    a00_high: [23780, 23800, 23810],
    alumina_avg: [2740, 2735, 2730],
    spread: [20940, 20965, 20980],
  },
  overlay: {
    dates: [
      '2026-10-20',
      '2025-10-20',
      '2026-10-21',
      '2025-10-21',
      '2026-10-22',
      '2025-10-22',
    ],
    values: [23680, 20500, 23700, 20550, 23710, 20600],
  },
  coverage: { trading_days: 3, target: 3, complete: true },
}

const inventoryCurve = {
  group: 'aluminumInventory',
  group_label: '铝锭库存',
  scope: 'Mysteel · 社会库存与厂内库存',
  as_of: '2026-10-22',
  dates: ['2026-10-15', '2026-10-19', '2026-10-22'],
  metrics: [
    {
      id: 'social',
      latest: 53.1,
      latest_date: '2026-10-22',
      delta: -3.5,
      delta_pct: -6.183,
    },
    {
      id: 'factory',
      latest: 7.7,
      latest_date: '2026-10-22',
      delta: -1.4,
      delta_pct: -15.385,
    },
    {
      id: 'total',
      latest: 60.8,
      latest_date: '2026-10-22',
      delta: -4.9,
      delta_pct: -7.458,
    },
  ],
  series: {
    social: [58.6, 56.6, 53.1],
    factory: [9.1, null, 7.7],
    total: [67.7, 65.7, 60.8],
    social_change: [null, -2.0, -3.5],
  },
  coverage: { trading_days: 3, target: 3, complete: true },
}

const threeGorgesCurve = {
  group: 'threeGorges',
  group_label: '三峡水库水位',
  scope: '湖北水文 · 三峡站 60106980',
  as_of: '2026-10-23 08',
  dates: ['2026-10-21', '2026-10-22', '2026-10-23'],
  metrics: [
    {
      id: 'level',
      latest: 174.77,
      latest_date: '2026-10-23',
      delta: -0.09,
      delta_pct: -0.0515,
    },
    {
      id: 'inflow',
      latest: 10900,
      latest_date: '2026-10-23',
      delta: -900,
      delta_pct: -7.6271,
    },
    {
      id: 'outflow',
      latest: 13400,
      latest_date: '2026-10-23',
      delta: 4240,
      delta_pct: 46.2882,
    },
  ],
  series: {
    level: [174.86, 174.86, 174.77],
    inflow: [11800, 11800, 10900],
    outflow: [9160, 9160, 13400],
  },
  coverage: { trading_days: 3, target: 3, complete: true },
}

const marginCurve = {
  group: 'margin',
  group_label: '融资融券',
  scope: '沪深两市',
  as_of: '2026-10-21',
  dates,
  metrics: [
    {
      id: 'rz_balance',
      latest: 24502.771794,
      latest_date: '2026-10-21',
      delta: -9.677585,
      delta_pct: -0.0395,
    },
    {
      id: 'rz_buy',
      latest: 1173.417709,
      latest_date: '2026-10-21',
      delta: -36.821142,
      delta_pct: -3.0425,
    },
    {
      id: 'rz_repay',
      latest: 1183.095295,
      latest_date: '2026-10-21',
      delta: -48.436576,
      delta_pct: -3.933,
    },
    {
      id: 'rq_volume',
      latest: 44.250815,
      latest_date: '2026-10-21',
      delta: 0.336741,
      delta_pct: 0.7668,
    },
    {
      id: 'rq_balance',
      latest: 288.540977,
      latest_date: '2026-10-21',
      delta: -2.475935,
      delta_pct: -0.8508,
    },
    {
      id: 'total_balance',
      latest: 24791.312771,
      latest_date: '2026-10-21',
      delta: -12.15352,
      delta_pct: -0.049,
    },
  ],
  series: {
    rz_balance: [24533.742399, 24512.44938, 24502.771794],
    rz_buy: [1182.386043, 1210.238851, 1173.417709],
    rz_repay: [1245.6996, 1231.53187, 1183.095295],
    rq_volume: [44.130114, 43.914073, 44.250815],
    rq_balance: [291.676273, 291.016912, 288.540977],
    total_balance: [24825.418672, 24803.466292, 24791.312771],
  },
  coverage: { trading_days: 3, target: 3, complete: true },
}

beforeEach(() => {
  vi.mocked(fetchAPI).mockReset()
  vi.mocked(fetchAPI).mockImplementation((path => {
    if (path === '/etf-shares/catalog') return Promise.resolve(catalog)
    if (path === '/etf-shares/coal/points') {
      return Promise.resolve({ date: '2026-10-15', price: 1020 })
    }
    if (String(path).startsWith('/etf-shares/coal/points/')) {
      return Promise.resolve({ date: '2026-10-14', price: 1020 })
    }
    if (path.includes('/curve?group=zz500')) return Promise.resolve(zz500Curve)
    if (path.includes('/curve?group=margin')) return Promise.resolve(marginCurve)
    if (path.includes('/curve?group=aluminumInventory')) {
      return Promise.resolve(inventoryCurve)
    }
    if (path.includes('/curve?group=threeGorges')) {
      return Promise.resolve(threeGorgesCurve)
    }
    if (path.includes('/curve?group=aluminum')) return Promise.resolve(aluminumCurve)
    if (path.includes('/curve?group=coalPrice')) return Promise.resolve(coalCurve)
    if (path.includes('/refresh')) return Promise.resolve(hs300Curve)
    return Promise.resolve(hs300Curve)
  }) as never)
})

afterEach(() => {
  cleanup()
})

describe('ETF share tracker page', () => {
  it('renders the group summary, fund cards, and total share chart', async () => {
    render(<EtfSharesPage />)

    expect((await screen.findAllByText('沪深300ETF华泰柏瑞')).length).toBeGreaterThan(0)
    expect(screen.getByText('301.99')).toBeTruthy()
    expect(screen.getByText(/\+7\.98亿份/)).toBeTruthy()
    expect(screen.getByText(/数据更新：2026-10-21/)).toBeTruthy()
    expect(screen.getByText('87 个交易日')).toBeTruthy()
    expect(screen.getByRole('img', { name: '总份额变化曲线（全部）' })).toBeTruthy()
    expect(screen.queryByText(/历史数据补齐中/)).toBeNull()

    expect(await screen.findByRole('heading', { name: '每日份额变化量' })).toBeTruthy()
    expect(
      screen.getByRole('img', { name: '每日份额变化量 沪深300ETF华泰柏瑞' }),
    ).toBeTruthy()
  })

  it('focuses the first fund by default and switches focus between cards', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    expect(await screen.findByRole('heading', { name: '每日份额变化量' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: '近10日份额变化' })).toBeTruthy()
    expect(
      screen.getByRole('img', { name: '每日份额变化量 沪深300ETF华泰柏瑞' }),
    ).toBeTruthy()
    expect(
      screen.getByRole('img', { name: '近10日份额变化 沪深300ETF华泰柏瑞' }),
    ).toBeTruthy()

    await user.click(screen.getAllByRole('button', { name: /沪深300ETF易方达/ })[0])
    expect(
      await screen.findByRole('img', { name: '每日份额变化量 沪深300ETF易方达' }),
    ).toBeTruthy()
    expect(
      screen.queryByRole('img', { name: '每日份额变化量 沪深300ETF华泰柏瑞' }),
    ).toBeNull()

    await user.click(screen.getAllByRole('button', { name: /沪深300ETF易方达/ })[0])
    expect(screen.queryByRole('heading', { name: '每日份额变化量' })).toBeNull()
  })

  it('switches to CSI 500 and requests that group', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    const entries = screen.getAllByRole('button', { name: '中证500 ETF' })
    await user.click(entries[0])

    expect((await screen.findAllByText('中证500ETF南方')).length).toBeGreaterThan(0)
    expect(
      await screen.findByRole('img', { name: '每日份额变化量 中证500ETF南方' }),
    ).toBeTruthy()
    expect(vi.mocked(fetchAPI)).toHaveBeenCalledWith(
      expect.stringContaining('group=zz500'),
      expect.anything(),
    )
  })

  it('renders the market-wide margin sentiment dashboard', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    const entries = screen.getAllByRole('button', { name: '融资融券' })
    await user.click(entries[0])

    expect(
      await screen.findByRole('heading', { name: '融资融券 · 市场情绪' }),
    ).toBeTruthy()
    expect(screen.getByText('沪深两市')).toBeTruthy()
    for (const label of [
      '融资余额',
      '融资买入额',
      '融资偿还额',
      '融券余量',
      '融券余量金额',
      '融资融券余额',
    ]) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    expect(screen.getAllByText('24502.77').length).toBeGreaterThan(0)
    expect(screen.getAllByText('44.25').length).toBeGreaterThan(0)
    expect(screen.getAllByText('亿元').length).toBeGreaterThan(0)
    expect(screen.getAllByText('亿股').length).toBeGreaterThan(0)
    expect(screen.getByRole('img', { name: '融资融券余额变化曲线' })).toBeTruthy()
    expect(screen.getByRole('img', { name: '融券余量变化' })).toBeTruthy()
    expect(screen.getByRole('img', { name: '融资买入额 vs 偿还额' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: '每日份额变化量' })).toBeNull()
    expect(
      vi.mocked(fetchAPI).mock.calls.some((call) =>
        String(call[0]).includes('group=margin'),
      ),
    ).toBe(true)
  })

  it('renders the CCI coal price dashboard', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    const entries = screen.getAllByRole('button', { name: '煤炭价格' })
    await user.click(entries[0])

    expect(
      await screen.findByRole('heading', { name: '煤炭价格 · CCI5500' }),
    ).toBeTruthy()
    expect(screen.getByText('现货')).toBeTruthy()
    expect(screen.getAllByText('CCI5500 现货').length).toBeGreaterThan(0)
    expect(screen.getAllByText('1018').length).toBeGreaterThan(0)
    expect(screen.getAllByText('元/吨').length).toBeGreaterThan(0)
    expect(
      screen.getByRole('img', { name: 'CCI5500 现货价格（按年叠加）' }),
    ).toBeTruthy()
    expect(screen.getByText('日度')).toBeTruthy()
    expect(screen.getByText('按年叠加')).toBeTruthy()
    expect(screen.getByText('2025')).toBeTruthy()
    expect(screen.getByText('2026')).toBeTruthy()
    expect(screen.queryByText('近一年')).toBeNull()
    expect(
      screen.getByText(
        /存放位置：\/srv\/panwatch\/data\/panwatch\.db · 表 coal_price_points/,
      ),
    ).toBeTruthy()
    expect(screen.getByText(/coal_cci_5500\.csv/)).toBeTruthy()
    expect(screen.queryByRole('heading', { name: '每日份额变化量' })).toBeNull()
    expect(
      vi.mocked(fetchAPI).mock.calls.some(
        (call) =>
          String(call[0]).includes('group=coalPrice') &&
          String(call[0]).includes('full=1'),
      ),
    ).toBe(true)
  })

  it('lists coal entries and adds a new price', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    await user.click(screen.getAllByRole('button', { name: '煤炭价格' })[0])

    expect(await screen.findByRole('heading', { name: '数据维护' })).toBeTruthy()
    expect(screen.getByText('共 5 条')).toBeTruthy()
    expect(screen.getAllByText('2026-10-14').length).toBeGreaterThan(0)
    expect(screen.getAllByText('1003').length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: /新增数据/ }))
    expect(
      await screen.findByRole('heading', { name: '新增煤炭价格' }),
    ).toBeTruthy()

    fireEvent.change(screen.getByLabelText('日期'), {
      target: { value: '2026-10-15' },
    })
    fireEvent.change(screen.getByLabelText('价格（元/吨）'), {
      target: { value: '1020' },
    })
    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() =>
      expect(vi.mocked(fetchAPI)).toHaveBeenCalledWith(
        '/etf-shares/coal/points',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ date: '2026-10-15', price: 1020 }),
        }),
      ),
    )
    await waitFor(() =>
      expect(
        screen.queryByRole('heading', { name: '新增煤炭价格' }),
      ).toBeNull(),
    )
    await waitFor(() =>
      expect(
        vi
          .mocked(fetchAPI)
          .mock.calls.filter(
            (call) =>
              String(call[0]).includes('group=coalPrice') &&
              String(call[0]).includes('full=1'),
          ).length,
      ).toBeGreaterThanOrEqual(2),
    )
  })

  it('edits an existing coal price with the original date', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    await user.click(screen.getAllByRole('button', { name: '煤炭价格' })[0])
    const editButtons = await screen.findAllByRole('button', { name: '编辑' })
    await user.click(editButtons[0])

    expect(
      await screen.findByRole('heading', { name: '编辑煤炭价格' }),
    ).toBeTruthy()
    const dateInput = screen.getByLabelText('日期') as HTMLInputElement
    const priceInput = screen.getByLabelText('价格（元/吨）') as HTMLInputElement
    expect(dateInput.value).toBe('2026-10-14')
    expect(priceInput.value).toBe('1018')

    await user.clear(priceInput)
    await user.type(priceInput, '1020')
    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() =>
      expect(vi.mocked(fetchAPI)).toHaveBeenCalledWith(
        '/etf-shares/coal/points/2026-10-14',
        expect.objectContaining({
          method: 'PUT',
          body: JSON.stringify({ date: '2026-10-14', price: 1020 }),
        }),
      ),
    )
  })

  it('blocks duplicate coal dates before calling the API', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    await user.click(screen.getAllByRole('button', { name: '煤炭价格' })[0])
    await screen.findByRole('heading', { name: '数据维护' })
    await user.click(screen.getByRole('button', { name: /新增数据/ }))

    fireEvent.change(screen.getByLabelText('日期'), {
      target: { value: '2026-10-14' },
    })
    fireEvent.change(screen.getByLabelText('价格（元/吨）'), {
      target: { value: '1020' },
    })
    await user.click(screen.getByRole('button', { name: '保存' }))

    expect(
      await screen.findByText('该日期已有煤炭价格，请直接编辑已有记录。'),
    ).toBeTruthy()
    expect(
      vi.mocked(fetchAPI).mock.calls.some(
        (call) => call[0] === '/etf-shares/coal/points',
      ),
    ).toBe(false)
  })

  it('renders the aluminum and alumina price dashboard', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    const entries = screen.getAllByRole('button', { name: '电解铝·氧化铝' })
    await user.click(entries[0])

    expect(
      await screen.findByRole('heading', { name: '电解铝 & 氧化铝 · 现货价格' }),
    ).toBeTruthy()
    for (const label of ['A00 铝均价', '氧化铝均价', '铝-氧化铝价差']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0)
    }
    expect(screen.getAllByText('23710').length).toBeGreaterThan(0)
    expect(screen.getAllByText('2730').length).toBeGreaterThan(0)
    expect(screen.getAllByText('20980').length).toBeGreaterThan(0)
    expect(screen.getAllByText('元/吨').length).toBeGreaterThan(0)
    expect(
      screen.queryByRole('img', { name: 'A00 铝价格走势（元/吨）' }),
    ).toBeNull()
    expect(
      screen.getByRole('img', { name: '氧化铝(山西)价格走势（元/吨）' }),
    ).toBeTruthy()
    expect(screen.getByRole('img', { name: '电解铝 vs 氧化铝价差' })).toBeTruthy()
    expect(screen.getByRole('img', { name: '价格对比（近20日）' })).toBeTruthy()
    expect(
      screen.getByRole('heading', { name: 'A00 铝价格走势图' }),
    ).toBeTruthy()
    expect(
      screen.getByRole('img', { name: 'A00 铝价格按年叠加走势' }),
    ).toBeTruthy()
    expect(screen.getAllByText('2025').length).toBeGreaterThan(0)
    expect(screen.getAllByText('2026').length).toBeGreaterThan(0)
    expect(screen.queryByRole('heading', { name: '每日份额变化量' })).toBeNull()
    expect(
      vi.mocked(fetchAPI).mock.calls.some((call) =>
        String(call[0]).includes('group=aluminum'),
      ),
    ).toBe(true)
  })

  it('renders the aluminum inventory dashboard with full history', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    const entries = screen.getAllByRole('button', { name: '铝锭库存' })
    await user.click(entries[0])

    expect(
      await screen.findByRole('heading', { name: '铝锭库存 · 社会与厂内' }),
    ).toBeTruthy()
    for (const label of ['社会库存', '厂内库存', '库存合计']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0)
    }
    expect(screen.getAllByText('53.1').length).toBeGreaterThan(0)
    expect(screen.getAllByText('7.7').length).toBeGreaterThan(0)
    expect(screen.getAllByText('60.8').length).toBeGreaterThan(0)
    expect(screen.getAllByText('万吨').length).toBeGreaterThan(0)
    expect(
      screen.getByRole('img', { name: '电解铝社会库存走势' }),
    ).toBeTruthy()
    expect(screen.getByRole('img', { name: '铝厂厂内库存走势' })).toBeTruthy()
    expect(screen.getByRole('img', { name: '社会库存每期变化' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: '每日份额变化量' })).toBeNull()
    expect(
      vi.mocked(fetchAPI).mock.calls.some((call) =>
        String(call[0]).includes('group=aluminumInventory&days=90&full=1'),
      ),
    ).toBe(true)
  })

  it('renders the Three Gorges reservoir dashboard', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    const entries = screen.getAllByRole('button', { name: '三峡水库水位' })
    await user.click(entries[0])

    expect(
      await screen.findByRole('heading', { name: '三峡水库水位 · 坝前' }),
    ).toBeTruthy()
    for (const label of ['库水位', '入库流量', '出库流量']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0)
    }
    expect(screen.getAllByText('174.77').length).toBeGreaterThan(0)
    expect(screen.getAllByText('10900').length).toBeGreaterThan(0)
    expect(screen.getAllByText('13400').length).toBeGreaterThan(0)
    expect(screen.getAllByText('立方米/秒').length).toBeGreaterThan(0)
    expect(screen.getByText(/数据更新：2026-10-23 08/)).toBeTruthy()
    expect(screen.getByText(/（共 3 天）/)).toBeTruthy()
    expect(screen.getByRole('combobox', { name: '近 90 天' })).toBeTruthy()
    expect(screen.getByRole('img', { name: '坝前水位走势' })).toBeTruthy()
    expect(screen.getByRole('img', { name: '入库 vs 出库流量' })).toBeTruthy()
    expect(screen.queryByText('蓄水量')).toBeNull()
    expect(screen.queryByText('亿立方米')).toBeNull()
    expect(screen.queryByRole('img', { name: '蓄水量走势' })).toBeNull()
    expect(screen.queryByRole('img', { name: '日水位变化' })).toBeNull()
    expect(screen.getByText(/湖北水文三峡站/)).toBeTruthy()
    expect(screen.queryByRole('heading', { name: '每日份额变化量' })).toBeNull()
    expect(
      vi.mocked(fetchAPI).mock.calls.some((call) =>
        String(call[0]).endsWith('group=threeGorges&days=90'),
      ),
    ).toBe(true)
  })

  it('renders a planned group without requesting share data', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    const entries = screen.getAllByRole('button', { name: '黄金储备' })
    await user.click(entries[0])

    expect(await screen.findByText(/该分组的数据源尚未接入/)).toBeTruthy()
    expect(screen.getAllByText('待接入').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /刷新数据/ })).toBeNull()
    expect(
      vi.mocked(fetchAPI).mock.calls.some((call) => String(call[0]).includes('group=goldReserves')),
    ).toBe(false)
  })

  it('refreshes through the POST endpoint and shows the backfill banner', async () => {
    const user = userEvent.setup()
    vi.mocked(fetchAPI).mockImplementation((path => {
      if (path === '/etf-shares/catalog') return Promise.resolve(catalog)
      if (path.includes('/refresh')) {
        return Promise.resolve({
          ...hs300Curve,
          coverage: { trading_days: 41, target: 90, complete: false },
        })
      }
      return Promise.resolve(hs300Curve)
    }) as never)
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    await user.click(screen.getByRole('button', { name: /刷新数据/ }))

    await waitFor(() =>
      expect(vi.mocked(fetchAPI)).toHaveBeenCalledWith(
        expect.stringContaining('/etf-shares/refresh'),
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    expect(await screen.findByText(/历史数据补齐中（41\/90 个交易日）/)).toBeTruthy()
  })
})
