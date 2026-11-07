import { cleanup, render, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MarketMapSnapshot } from '@panwatch/api'
import { discoveryApi } from '@panwatch/api'
import MarketCloudMap from '@/components/MarketCloudMap'
import { MarketColorProvider } from '@/hooks/use-market-colors'

vi.mock('@panwatch/api', () => ({
  discoveryApi: { marketMapSector: vi.fn() },
}))

const snapshot: MarketMapSnapshot = {
  items: [
    {
      symbol: '601939', market: 'CN', name: '建设银行', price: 11.91, change_pct: 2.14,
      change_amount: 0.25, total_market_value: 3115660543177, industry: '银行Ⅱ',
    },
    {
      symbol: '601398', market: 'CN', name: '工商银行', price: 8.44, change_pct: -1.5,
      change_amount: -0.13, total_market_value: 3008068809831, industry: '银行Ⅱ',
    },
    {
      symbol: '688825', market: 'CN', name: '长鑫科技', price: 40.41, change_pct: -4.98,
      change_amount: -2.12, total_market_value: 2743196000000, industry: '半导体',
    },
    {
      symbol: '600519', market: 'CN', name: '贵州茅台', price: 1453, change_pct: 0.07,
      change_amount: 1.0, total_market_value: 1816369000000, industry: '白酒Ⅱ',
    },
  ],
  stats: { total: 5304, up: 726, flat: 73, down: 4505, amount: 1_593_981_035_794.96, as_of: 1793950482 },
}

function renderMap(overrides: Partial<React.ComponentProps<typeof MarketCloudMap>> = {}) {
  return render(
    <MarketColorProvider>
      <MarketCloudMap
        data={snapshot}
        loading={false}
        error={false}
        onSelectStock={vi.fn()}
        {...overrides}
      />
    </MarketColorProvider>,
  )
}

beforeEach(() => {
  vi.mocked(discoveryApi.marketMapSector).mockImplementation(async ({ industry }) => {
    const items = snapshot.items.filter((item) => (item.industry || '').trim() === industry)
    return { items, count: items.length }
  })
  // jsdom 不做布局，容器测量恒为 0；这里给出固定视口尺寸让云图铺满。
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 900 })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 300 })
})

afterEach(() => {
  cleanup()
  delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth
  delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight
  localStorage.clear()
})

describe('MarketCloudMap', () => {
  it('draws one tile per stock and groups them by industry', () => {
    const { container, getAllByText } = renderMap()
    const tiles = container.querySelectorAll('button')

    expect(tiles).toHaveLength(snapshot.items.length)
    expect(tiles[0].textContent).toContain('建设银行')
    expect(tiles[0].textContent).toContain('+2.14%')
    expect(tiles[1].textContent).toContain('工商银行')
    expect(tiles[1].textContent).toContain('-1.50%')
    expect(getAllByText('银行Ⅱ').length).toBeGreaterThan(0)
    expect(getAllByText('半导体').length).toBeGreaterThan(0)
    expect(getAllByText('白酒Ⅱ').length).toBeGreaterThan(0)
    for (const tile of tiles) {
      expect(Number.parseFloat((tile as HTMLElement).style.width)).toBeGreaterThan(0)
      expect(Number.parseFloat((tile as HTMLElement).style.height)).toBeGreaterThan(0)
    }
  })

  it('shows full-market breadth, turnover and coverage', () => {
    const { getByText } = renderMap()

    expect(getByText('726')).toBeTruthy()
    expect(getByText('73')).toBeTruthy()
    expect(getByText('4505')).toBeTruthy()
    expect(getByText(/展示 4 \/ 5304 只/)).toBeTruthy()
  })

  it('colors gains with the up palette and losses with the down palette', () => {
    const { container } = renderMap()
    const [gain, loss] = container.querySelectorAll('button') as NodeListOf<HTMLElement>

    expect(gain.style.backgroundColor).toMatch(/^rgb\(/)
    expect(loss.style.backgroundColor).toMatch(/^rgb\(/)
    expect(gain.style.backgroundColor).not.toBe(loss.style.backgroundColor)
    expect(gain.style.color).toBeTruthy()
  })

  it('shows the hovered stock and its whole sector on hover', async () => {
    const { container, getByRole, queryByRole } = renderMap()
    const tiles = container.querySelectorAll<HTMLElement>('button[aria-label]')
    const tile = tiles[0]

    await userEvent.hover(tile)
    const tip = getByRole('tooltip')
    expect(tip.textContent).toContain('银行Ⅱ')
    expect(tip.textContent).toContain('建设银行')
    expect(tip.textContent).toContain('601939')
    expect(tip.textContent).toContain('11.91')
    expect(tip.textContent).toContain('+0.25')
    expect(tip.textContent).toContain('+2.14%')
    expect(tip.textContent).toContain('上涨')
    expect(tip.textContent).toContain('下跌')
    // 整块板块的个股列表
    expect(tip.textContent).toContain('板块个股')
    expect(tip.textContent).toContain('工商银行')
    expect(tip.textContent).toContain('-1.50%')

    // 悬停时整个行业高亮（黄色描边），其他行业不受影响。
    await waitFor(() => expect(tile.style.outline).toContain('255, 214, 20'))
    const otherTile = tiles[tiles.length - 1]
    expect(otherTile.style.outline).toBe('')

    await userEvent.unhover(tile)
    await waitFor(() => expect(queryByRole('tooltip')).toBeNull())
  })

  it('lists every stock in the sector from the full sector endpoint', async () => {
    const banks = Array.from({ length: 12 }, (_, i) => ({
      symbol: `60000${i}`,
      market: 'CN',
      name: `银行${i}`,
      price: 10 + i,
      change_pct: i % 2 ? 1 : -1,
      change_amount: 0.1,
      total_market_value: 1e10 - i * 1e8,
      industry: '银行Ⅱ',
    }))
    vi.mocked(discoveryApi.marketMapSector).mockResolvedValue({ items: banks, count: banks.length })
    const { container, getByRole } = renderMap({
      data: { items: banks.slice(0, 2), stats: snapshot.stats },
    })

    await userEvent.hover(container.querySelectorAll('button[aria-label]')[0])
    const tip = getByRole('tooltip')
    const list = within(tip.querySelector('.scrollbar') as HTMLElement)
    await waitFor(() => expect(list.getAllByRole('button')).toHaveLength(banks.length))
    expect(tip.textContent).toContain('银行11')
    expect(tip.textContent).toContain('12')
  })

  it('sorts the sector list by change percent and toggles direction', async () => {
    const pcts = [1, -2, 3, 0, -1]
    const banks = pcts.map((pct, i) => ({
      symbol: `60000${i}`,
      market: 'CN',
      name: `银行${i}`,
      price: 10 + i,
      change_pct: pct,
      change_amount: pct / 10,
      total_market_value: 1e10 - i * 1e8,
      industry: '银行Ⅱ',
    }))
    vi.mocked(discoveryApi.marketMapSector).mockResolvedValue({ items: banks, count: banks.length })
    const { container, getByRole } = renderMap({
      data: { items: banks.slice(0, 2), stats: snapshot.stats },
    })

    await userEvent.hover(container.querySelectorAll('button[aria-label]')[0])
    const tip = getByRole('tooltip')
    const list = within(tip.querySelector('.scrollbar') as HTMLElement)
    await waitFor(() => expect(list.getAllByRole('button')).toHaveLength(banks.length))
    const names = () => list.getAllByRole('button').map((row) => row.textContent || '')
    expect(names()[0]).toContain('银行2')
    expect(names()[names().length - 1]).toContain('银行1')

    await userEvent.click(within(tip).getByRole('button', { name: '涨跌幅 ↓' }))
    await waitFor(() => expect(names()[0]).toContain('银行1'))
    expect(names()[names().length - 1]).toContain('银行2')
  })

  it('opens the requested stock when a tile is clicked', async () => {
    const onSelectStock = vi.fn()
    const { container } = renderMap({ onSelectStock })

    await userEvent.click(container.querySelectorAll('button')[1])
    expect(onSelectStock).toHaveBeenCalledWith('601398', 'CN', '工商银行')
  })

  it('shows loading, error and empty states', () => {
    const loading = renderMap({ data: null, loading: true })
    expect(loading.getByText('云图加载中…')).toBeTruthy()
    loading.unmount()

    const error = renderMap({ data: null, error: true })
    expect(error.getByText('云图数据暂不可用')).toBeTruthy()
    error.unmount()

    const empty = renderMap({ data: null })
    expect(empty.getByText('暂无云图数据')).toBeTruthy()
  })
})