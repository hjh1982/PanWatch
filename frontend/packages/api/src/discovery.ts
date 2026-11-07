import { fetchAPI } from './client'

type QueryValue = string | number | boolean | null | undefined

function withQuery(path: string, params: Record<string, QueryValue>): string {
  const q = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v === undefined || v === null) return
    const sv = String(v).trim()
    if (!sv) return
    q.set(k, sv)
  })
  const s = q.toString()
  return s ? `${path}?${s}` : path
}

export interface HotStockItem {
  symbol: string
  market: string
  name: string
  price: number | null
  change_pct: number | null
  turnover: number | null
  volume?: number | null
}

export interface HotBoardItem {
  code: string
  name: string
  change_pct: number | null
  turnover: number | null
}

export interface MarketMapItem {
  symbol: string
  market: string
  name: string
  price: number | null
  change_pct: number | null
  total_market_value: number | null
  industry: string | null
  change_amount?: number | null
}

export interface MarketMapStats {
  total: number
  up: number
  flat: number
  down: number
  amount: number | null
  as_of: number | null
}

export interface MarketMapSnapshot {
  items: MarketMapItem[]
  stats: MarketMapStats
}

export const discoveryApi = {
  marketMap: (params?: { market?: 'CN'; limit?: number }) =>
    fetchAPI<MarketMapSnapshot>(
      withQuery('/discovery/market-map', {
        market: params?.market,
        limit: params?.limit,
      })
    ),

  marketMapSector: (params: { industry: string; market?: 'CN' }) =>
    fetchAPI<{ items: MarketMapItem[]; count: number }>(
      withQuery('/discovery/market-map/sector', {
        industry: params.industry,
        market: params.market,
      })
    ),

  listHotStocks: (params?: {
    market?: 'CN' | 'HK' | 'US'
    mode?: 'turnover' | 'gainers' | 'for_you'
    limit?: number
  }) =>
    fetchAPI<HotStockItem[]>(
      withQuery('/discovery/stocks', {
        market: params?.market,
        mode: params?.mode,
        limit: params?.limit,
      })
    ),

  listHotBoards: (params?: {
    market?: 'CN' | 'HK' | 'US'
    mode?: 'gainers' | 'turnover' | 'hot'
    limit?: number
  }) =>
    fetchAPI<HotBoardItem[]>(
      withQuery('/discovery/boards', {
        market: params?.market,
        mode: params?.mode,
        limit: params?.limit,
      })
    ),

  listBoardStocks: (
    boardCode: string,
    params?: {
      mode?: 'gainers' | 'turnover' | 'hot'
      limit?: number
    }
  ) =>
    fetchAPI<HotStockItem[]>(
      withQuery(`/discovery/boards/${encodeURIComponent(boardCode)}/stocks`, {
        mode: params?.mode,
        limit: params?.limit,
      })
    ),
}

