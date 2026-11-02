import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { recommendationsApi, type CashOptionPayload } from '@panwatch/api'
import CashOptionPanel from '@/components/CashOptionPanel'

vi.mock('@panwatch/api', () => ({
  recommendationsApi: {
    getCashOptions: vi.fn(),
    refreshCashOptions: vi.fn(),
  },
}))

const payload: CashOptionPayload = {
  as_of: '2026-11-02',
  summary: {
    active: 1,
    upcoming: 0,
    recent_ended: 1,
    best_premium_pct: 14.07,
  },
  events: [
    {
      code: '601059',
      name: '信达证券',
      market: 'SH',
      method: 'merger',
      exercise_price: 17.75,
      quote_price: 15.56,
      quote_date: '2026-10-30',
      reference_price: 15.56,
      basis: 'quote',
      premium_pct: 14.07,
      annualized_pct: 286.42,
      record_date: '2026-09-14',
      window_start: '2026-09-15',
      window_end: '2026-11-20',
      status: 'active',
      days_left: 18,
      eligibility: 'dissenting',
      apply_code: '770008',
      announcement_title: '信达证券A股异议股东现金选择权实施公告',
      announcement_date: '2026-09-14',
      announcement_url: 'http://static.cninfo.com.cn/finalpage/2026-09-15/1225563651.PDF',
    },
    {
      code: '000016',
      name: '*ST康佳A',
      market: 'SZ',
      method: 'delisting',
      exercise_price: 2.48,
      quote_price: null,
      quote_date: null,
      reference_price: 2.46,
      basis: 'reference',
      premium_pct: 0.81,
      annualized_pct: null,
      record_date: '2026-09-22',
      window_start: '2026-09-30',
      window_end: '2026-10-13',
      status: 'ended',
      days_left: null,
      eligibility: 'all',
      apply_code: '038043',
      announcement_title: '关于主动终止公司股票上市现金选择权行权暨实施的公告',
      announcement_date: '2026-09-23',
      announcement_url: 'http://static.cninfo.com.cn/finalpage/2026-09-24/1225580018.PDF',
    },
  ],
  error: '',
  sources: [
    {
      id: 'cninfo',
      name: '巨潮资讯 · 现金选择权公告',
      url: 'https://www.cninfo.com.cn/new/fulltextSearch',
      cadence: '事件驱动',
      latest: '2026-09-23',
    },
  ],
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('CashOptionPanel', () => {
  it('展示汇总、事件详情与公告链接', async () => {
    vi.mocked(recommendationsApi.getCashOptions).mockResolvedValue(payload)

    render(<CashOptionPanel />)

    expect(await screen.findByText('信达证券')).toBeTruthy()
    expect(screen.getByText('*ST康佳A')).toBeTruthy()
    expect(screen.getAllByText('申报中').length).toBeGreaterThan(0)
    expect(screen.getByText('已结束')).toBeTruthy()
    expect(screen.getByText('换股合并')).toBeTruthy()
    expect(screen.getByText('主动终止上市')).toBeTruthy()
    expect(screen.getByText('异议股东')).toBeTruthy()
    expect(screen.getByText('全体股东（终止上市）')).toBeTruthy()
    expect(screen.getAllByText('+14.07%').length).toBeGreaterThan(0)
    expect(screen.getByText('+0.81%')).toBeTruthy()
    expect(screen.getByText('17.75')).toBeTruthy()
    expect(screen.getByText('15.56')).toBeTruthy()
    expect(screen.getAllByText('按公告收盘价').length).toBeGreaterThan(0)
    expect(screen.getByText('770008')).toBeTruthy()
    expect(screen.getByText('038043')).toBeTruthy()
    expect(screen.getByText('剩余 18 天')).toBeTruthy()
    expect(screen.getByText('2026-09-15 ~ 2026-11-20')).toBeTruthy()
    expect(screen.getByText('已结束事件仅作参考，不可再申报')).toBeTruthy()
    expect(screen.getByText('截至 2026-11-02')).toBeTruthy()

    const link = screen.getByTitle('信达证券A股异议股东现金选择权实施公告')
    expect(link.getAttribute('href')).toBe('http://static.cninfo.com.cn/finalpage/2026-09-15/1225563651.PDF')
    expect(link.getAttribute('target')).toBe('_blank')

    expect(screen.getByText(/巨潮资讯 · 现金选择权公告/)).toBeTruthy()
    expect(screen.getByText(/事件驱动/)).toBeTruthy()
  })

  it('无事件时展示空状态', async () => {
    vi.mocked(recommendationsApi.getCashOptions).mockResolvedValue({
      ...payload,
      summary: { active: 0, upcoming: 0, recent_ended: 0, best_premium_pct: null },
      events: [],
    })

    render(<CashOptionPanel />)

    expect(
      await screen.findByText('近 120 天没有进行中、即将开始或刚结束的现金选择权事件')
    ).toBeTruthy()
    expect(screen.getByText('最佳溢价')).toBeTruthy()
  })

  it('抓取失败时给出提示', async () => {
    vi.mocked(recommendationsApi.getCashOptions).mockResolvedValue({
      ...payload,
      summary: { active: 0, upcoming: 0, recent_ended: 0, best_premium_pct: null },
      events: [],
      error: 'fetch_failed',
    })

    render(<CashOptionPanel />)

    expect(await screen.findByText('巨潮公告抓取失败，请稍后重试')).toBeTruthy()
  })

  it('刷新按钮走强制刷新接口', async () => {
    vi.mocked(recommendationsApi.getCashOptions).mockResolvedValue(payload)
    vi.mocked(recommendationsApi.refreshCashOptions).mockResolvedValue(payload)

    render(<CashOptionPanel />)
    await screen.findByText('信达证券')

    await userEvent.click(screen.getByRole('button', { name: '刷新公告' }))

    await waitFor(() => {
      expect(recommendationsApi.refreshCashOptions).toHaveBeenCalledTimes(1)
    })
    expect(recommendationsApi.getCashOptions).toHaveBeenCalledTimes(1)
  })
})
