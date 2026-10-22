import { cleanup, render, screen, waitFor } from '@testing-library/react'
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
      available: false,
      accent: '#94a3b8',
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
    if (path.includes('/curve?group=zz500')) return Promise.resolve(zz500Curve)
    if (path.includes('/curve?group=margin')) return Promise.resolve(marginCurve)
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

  it('renders a planned group without requesting share data', async () => {
    const user = userEvent.setup()
    render(<EtfSharesPage />)
    await screen.findAllByText('沪深300ETF华泰柏瑞')

    const entries = screen.getAllByRole('button', { name: '煤炭价格' })
    await user.click(entries[0])

    expect(await screen.findByText(/该分组的数据源尚未接入/)).toBeTruthy()
    expect(screen.getAllByText('待接入').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /刷新数据/ })).toBeNull()
    expect(
      vi.mocked(fetchAPI).mock.calls.some((call) => String(call[0]).includes('group=coalPrice')),
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
