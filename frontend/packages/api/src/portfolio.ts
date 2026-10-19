import { fetchAPI } from './client'

export interface PortfolioDiagnostics {
  position_count: number
  total_market_value: number
  hhi: number
  max_weight: number
  by_market: Record<string, number>
  by_strategy: Record<string, number>
  total_unrealized_pnl: number
  alerts: string[]
  alert_details?: Array<{
    code: 'single_concentration' | 'hhi_concentration' | 'too_few_positions' | 'market_concentration' | string
    weight?: number
    hhi?: number
    count?: number
    market?: string
  }>
}

export interface BenchmarkCurvePoint {
  date: string
  portfolio: number
  benchmark: number
}

export interface PortfolioBenchmark {
  empty?: boolean
  reason?: string
  portfolio_return?: number
  benchmark_return?: number
  excess_return?: number
  information_ratio?: number
  relative_drawdown?: number
  days?: number
  benchmark_code?: string
  benchmark_label?: string
  curve?: BenchmarkCurvePoint[]
}

export const portfolioApi = {
  /** 真实持仓组合诊断(集中度/分布/风险提示)。 */
  diagnostics: () => fetchAPI<PortfolioDiagnostics>('/portfolio/diagnostics'),

  /** 组合 vs 基准(超额/信息比率/相对回撤 + 归一化曲线)。 */
  benchmark: (params?: { days?: number; benchmark?: string }) =>
    fetchAPI<PortfolioBenchmark>(
      `/portfolio/benchmark?days=${params?.days ?? 60}&benchmark=${encodeURIComponent(params?.benchmark ?? '000300')}`,
      { timeoutMs: 60000 },
    ),

  /** 个股对组合收益的贡献(谁拖累/贡献)。 */
  attribution: (days = 60) =>
    fetchAPI<{ items: AttributionItem[] }>(`/portfolio/attribution?days=${days}`, { timeoutMs: 60000 }),

  /** 组合 AI 体检(叙述结论 + 调仓建议)。 */
  aiReview: () => fetchAPI<PortfolioAiReview>('/portfolio/ai-review', { method: 'POST', timeoutMs: 60000 }),
}

export interface AttributionItem {
  symbol: string
  name: string
  market: string
  return_pct: number
  weight_pct: number
  contribution_pct: number
}

export interface PortfolioAiReview {
  empty?: boolean
  reason?: string
  content?: string
  top?: AttributionItem[]
  worst?: AttributionItem[]
}

export interface PortfolioIntradayPoint {
  time: string
  market_value: number
  total_assets: number
}

export interface PortfolioIntradayCurve {
  empty?: boolean
  reason?: string
  session_date?: string | null
  points?: PortfolioIntradayPoint[]
  summary?: {
    current_market_value: number
    baseline_market_value: number
    change: number
    change_pct: number
    high: number
    high_time: string
    low: number
    low_time: string
    cash: number
  } | null
  coverage?: {
    positions: number
    intraday: number
    flat: string[]
    markets: string[]
  }
  account_id?: number | null
  currency?: string
  refreshed_at?: string
}
