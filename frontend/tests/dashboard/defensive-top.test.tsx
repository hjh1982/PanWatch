import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { dashboardApi, discoveryApi, fetchAPI, homeApi, portfolioApi } from '@panwatch/api'
import DashboardPage from '@/pages/Dashboard'
import { MarketColorProvider } from '@/hooks/use-market-colors'

vi.mock('@panwatch/api', () => ({
  dashboardApi: {
    indices: vi.fn(), commodities: vi.fn(), fearGreed: vi.fn(), defensiveTop: vi.fn(),
    intradaySnapshot: vi.fn(), watchlist: vi.fn(), portfolioSummary: vi.fn(), marketStatus: vi.fn(),
    brief: vi.fn(), curate: vi.fn(),
  },
  portfolioApi: { diagnostics: vi.fn(), benchmark: vi.fn(), attribution: vi.fn() },
  discoveryApi: { marketMap: vi.fn(), marketMapSector: vi.fn() },
  homeApi: { alertHitsToday: vi.fn(), todos: vi.fn() },
  fetchAPI: vi.fn(),
}))
vi.mock('@/components/DiscoveryPanel', () => ({ default: () => null }))
vi.mock('@panwatch/biz-ui/components/onboarding', () => ({ Onboarding: () => null }))

const payload = {
  as_of: '2026-11-20',
  stocks: [
    { symbol: '601088', name: '中国神华', available: true, close: 45.5, change_pct: -2.1, high60: 52, high60_date: '2026-11-03', drawdown_pct: -12.5, vs_ma20_pct: -3.2, vs_ma60_pct: -1.1, ma20_slope_pct: -0.6, ma60_slope_pct: 0.1, spark: [50, 49, 48] },
    { symbol: '600900', name: '长江电力', available: true, close: 29, change_pct: -1.2, high60: 31.6, high60_date: '2026-11-20', drawdown_pct: -8.2, vs_ma20_pct: -1.5, vs_ma60_pct: 0.2, ma20_slope_pct: -0.3, ma60_slope_pct: 0.2, spark: [31, 30, 29] },
    { symbol: '601398', name: '工商银行', available: true, close: 9.4, change_pct: 0.5, high60: 9.4, high60_date: '2026-11-20', drawdown_pct: 0, vs_ma20_pct: 7.6, vs_ma60_pct: 13.66, ma20_slope_pct: 2, ma60_slope_pct: 1.65, spark: [9, 9.2, 9.4] },
  ],
  collective: { warning: true, triggered: 2, total: 3, threshold: 2, drawdown_pct: 8, symbols: ['601088', '600900'] },
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  localStorage.setItem('panwatch_onboarding_completed', 'true')
  for (const api of [dashboardApi, discoveryApi, homeApi, portfolioApi]) {
    for (const method of Object.values(api)) vi.mocked(method).mockReturnValue(new Promise(() => {}))
  }
  vi.mocked(fetchAPI).mockReturnValue(new Promise(() => {}))
  vi.mocked(dashboardApi.curate).mockResolvedValue({ items: [] })
  vi.mocked(dashboardApi.defensiveTop).mockResolvedValue(payload)
})
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals() })

it('renders the three giants and the collective topping warning', async () => {
  render(
    <MarketColorProvider>
      <MemoryRouter><DashboardPage /></MemoryRouter>
    </MarketColorProvider>,
  )

  expect(await screen.findByText('高股息三巨头 · 做头观察')).toBeTruthy()
  expect(await screen.findByText(/防御龙头集体走弱/)).toBeTruthy()
  expect(screen.getByText('中国神华')).toBeTruthy()
  expect(screen.getByText('长江电力')).toBeTruthy()
  expect(screen.getByText('工商银行')).toBeTruthy()
  expect(screen.getByText(/距高点回撤 ≥8%/)).toBeTruthy()
})
