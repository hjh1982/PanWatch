import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { useTranslation } from 'react-i18next'
import { Database, LineChart, Loader2, Pencil, Plus, RefreshCw } from 'lucide-react'
import { fetchAPI } from '@panwatch/api/client'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { Button } from '@panwatch/base-ui/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@panwatch/base-ui/components/ui/dialog'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@panwatch/base-ui/components/ui/select'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { marketColorWithAlpha, marketSignTextClass } from '@/lib/market-colors'

interface FundMeta {
  code: string
  name: string
}

interface GroupMeta {
  id: string
  label: string
  category: 'broad' | 'commodity'
  available: boolean
  accent: string
  funds: FundMeta[]
}

interface Catalog {
  default_group: string
  default_days: number
  allowed_days: number[]
  groups: GroupMeta[]
}

interface Coverage {
  trading_days: number
  target: number
  complete: boolean
}

interface FundSeries extends FundMeta {
  latest: number | null
  latest_date: string | null
  delta: number | null
  delta_pct: number | null
  values: Array<number | null>
}

interface ShareCurvePayload {
  group: string
  group_label: string
  as_of: string | null
  dates: string[]
  funds: FundSeries[]
  coverage: Coverage
}

interface MarginMetric {
  id: string
  latest: number | null
  latest_date: string | null
  delta: number | null
  delta_pct: number | null
}

interface CoalStorage {
  database: string
  table: string
  seed: string
}

interface MetricsPayload {
  group: string
  group_label: string
  scope: string
  as_of: string | null
  dates: string[]
  metrics: MarginMetric[]
  series: Record<string, Array<number | null>>
  overlay?: {
    dates: string[]
    values: Array<number | null>
  }
  coverage: Coverage
  storage?: CoalStorage
}

type CurveResponse = ShareCurvePayload | MetricsPayload

interface CoalPoint {
  date: string
  price: number
}

function isMetricsPayload(
  payload: CurveResponse | null | undefined,
): payload is MetricsPayload {
  return Boolean(payload && 'metrics' in payload && 'series' in payload)
}

const etfSharesApi = {
  catalog: () => fetchAPI<Catalog>('/etf-shares/catalog'),
  curve: (group: string, days: number, full = false) =>
    fetchAPI<CurveResponse>(
      `/etf-shares/curve?group=${encodeURIComponent(group)}&days=${days}${full ? '&full=1' : ''}`,
      { timeoutMs: 90_000 },
    ),
  refresh: (group: string, days: number, full = false) =>
    fetchAPI<CurveResponse>(
      `/etf-shares/refresh?group=${encodeURIComponent(group)}&days=${days}${full ? '&full=1' : ''}`,
      { method: 'POST', timeoutMs: 120_000 },
    ),
  createCoalPoint: (point: CoalPoint) =>
    fetchAPI<CoalPoint>('/etf-shares/coal/points', {
      method: 'POST',
      body: JSON.stringify(point),
    }),
  updateCoalPoint: (originalDay: string, point: CoalPoint) =>
    fetchAPI<CoalPoint>(
      `/etf-shares/coal/points/${encodeURIComponent(originalDay)}`,
      { method: 'PUT', body: JSON.stringify(point) },
    ),
}

function localDateString(): string {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 10)
}

const SERIES_COLORS = ['#e5484d', '#3b82f6', '#f59e0b', '#0ea5a4', '#a855f7']
const DEFAULT_FOCUS_GROUPS = new Set(['hs300', 'zz500'])
const MARGIN_METRIC_COLORS: Record<string, string> = {
  rz_balance: '#2563eb',
  rz_buy: '#0ea5e9',
  rz_repay: '#f59e0b',
  rq_volume: '#ef4444',
  rq_balance: '#f43f5e',
  total_balance: '#8b5cf6',
}
const MARGIN_METRIC_KEYS = {
  rz_balance: 'etfShares.margin.metrics.rz_balance',
  rz_buy: 'etfShares.margin.metrics.rz_buy',
  rz_repay: 'etfShares.margin.metrics.rz_repay',
  rq_volume: 'etfShares.margin.metrics.rq_volume',
  rq_balance: 'etfShares.margin.metrics.rq_balance',
  total_balance: 'etfShares.margin.metrics.total_balance',
} as const
const COAL_METRIC_COLORS: Record<string, string> = {
  cci_5500: '#059669',
}
const COAL_METRIC_KEYS = {
  cci_5500: 'etfShares.coal.metrics.cci_5500',
} as const
const ALUMINUM_METRIC_COLORS: Record<string, string> = {
  a00_avg: '#e5484d',
  alumina_avg: '#0ea5e9',
  spread: '#f97316',
}
const ALUMINUM_METRIC_KEYS = {
  a00_avg: 'etfShares.aluminum.metrics.a00_avg',
  alumina_avg: 'etfShares.aluminum.metrics.alumina_avg',
  spread: 'etfShares.aluminum.metrics.spread',
} as const
const ALUMINUM_A00_COLORS = {
  avg: '#e5484d',
}
const ALUMINUM_INVENTORY_METRIC_COLORS: Record<string, string> = {
  social: '#0ea5e9',
  factory: '#f59e0b',
  total: '#059669',
}
const ALUMINUM_INVENTORY_METRIC_KEYS = {
  social: 'etfShares.aluminumInventory.metrics.social',
  factory: 'etfShares.aluminumInventory.metrics.factory',
  total: 'etfShares.aluminumInventory.metrics.total',
} as const
const THREE_GORGES_METRIC_COLORS: Record<string, string> = {
  level: '#0284c7',
  inflow: '#f59e0b',
  outflow: '#6366f1',
}
const THREE_GORGES_METRIC_KEYS = {
  level: 'etfShares.threeGorges.metrics.level',
  inflow: 'etfShares.threeGorges.metrics.inflow',
  outflow: 'etfShares.threeGorges.metrics.outflow',
} as const
const THREE_GORGES_METRIC_UNITS = {
  level: 'etfShares.threeGorges.units.level',
  inflow: 'etfShares.threeGorges.units.flow',
  outflow: 'etfShares.threeGorges.units.flow',
} as const
const YEAR_COLORS = ['#94a3b8', '#8b5cf6', '#3b82f6', '#f59e0b', '#059669']
const GROUP_LABEL_KEYS = {
  hs300: 'etfShares.groups.hs300',
  zz500: 'etfShares.groups.zz500',
  margin: 'etfShares.groups.margin',
  coalPrice: 'etfShares.groups.coalPrice',
  aluminum: 'etfShares.groups.aluminum',
  aluminumInventory: 'etfShares.groups.aluminumInventory',
  goldReserves: 'etfShares.groups.goldReserves',
  scfi: 'etfShares.groups.scfi',
  hydropower: 'etfShares.groups.hydropower',
  threeGorges: 'etfShares.groups.threeGorges',
} as const
const CHART_WIDTH = 880
const CHART_HEIGHT = 300
const CHART_PADDING = { top: 24, right: 18, bottom: 30, left: 56 }
const DETAIL_WIDTH = 560
const DETAIL_HEIGHT = 230
const DETAIL_PADDING = { top: 18, right: 16, bottom: 28, left: 52 }
const POLL_INTERVAL_MS = 1200

interface ChartSeries {
  code: string
  name: string
  color: string
  values: Array<number | null>
  dashed?: boolean
  filled?: boolean
}

function seriesColor(index: number): string {
  if (!Number.isFinite(index) || index < 0) return SERIES_COLORS[0]
  return SERIES_COLORS[Math.trunc(index) % SERIES_COLORS.length]
}

function focusAfterLoad(group: string, funds: FundSeries[], current: string | null): string | null {
  if (current && funds.some((fund) => fund.code === current)) return current
  return DEFAULT_FOCUS_GROUPS.has(group) ? funds[0]?.code ?? null : null
}

function formatValue(value: number | null, digits = 2): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '--'
  return value.toFixed(digits)
}

function formatDelta(value: number | null, digits = 2): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '--'
  const magnitude = Math.abs(value).toFixed(digits)
  return `${value < 0 ? '-' : '+'}${magnitude}`
}

function formatPercent(value: number | null, digits = 2): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '--'
  const magnitude = Math.abs(value).toFixed(digits)
  return `${value < 0 ? '-' : '+'}${magnitude}%`
}

function niceStep(raw: number): number {
  if (!Number.isFinite(raw) || raw <= 0) return 1
  const exponent = Math.floor(Math.log10(raw))
  const fraction = raw / 10 ** exponent
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10
  return nice * 10 ** exponent
}

function buildScale(values: Array<number | null>, targetTicks = 5) {
  const finite = values.filter(
    (value): value is number => typeof value === 'number' && Number.isFinite(value),
  )
  if (finite.length === 0) return null
  const min = Math.min(...finite)
  const max = Math.max(...finite)
  const span = max - min
  const pad = span > 0 ? span * 0.08 : Math.max(Math.abs(max) * 0.05, 0.5)
  const step = niceStep((span + pad * 2) / Math.max(2, targetTicks - 1))
  const lower = Math.floor((min - pad) / step) * step
  const upper = Math.ceil((max + pad) / step) * step
  const ticks: number[] = []
  for (let tick = lower; tick <= upper + step / 2; tick += step) {
    ticks.push(Number(tick.toFixed(6)))
  }
  return { min: lower, max: upper, step, ticks }
}

function pickTickIndexes(total: number, maxTicks: number): number[] {
  if (!Number.isFinite(total) || total <= 0) return []
  if (total === 1) return [0]
  const count = Math.max(2, Math.trunc(maxTicks))
  if (total <= count) return Array.from({ length: total }, (_, index) => index)
  const indexes = new Set<number>([0, total - 1])
  const inner = count - 1
  for (let index = 1; index < inner; index += 1) {
    indexes.add(Math.round((index * (total - 1)) / inner))
  }
  return [...indexes].sort((left, right) => left - right)
}

function buildSegments(values: Array<number | null>): Array<Array<{ index: number; value: number }>> {
  const segments: Array<Array<{ index: number; value: number }>> = []
  let current: Array<{ index: number; value: number }> = []
  values.forEach((value, index) => {
    if (typeof value === 'number' && Number.isFinite(value)) {
      current.push({ index, value })
    } else if (current.length > 0) {
      segments.push(current)
      current = []
    }
  })
  if (current.length > 0) segments.push(current)
  return segments
}

function tickLabel(value: number, step: number): string {
  return value.toFixed(step < 1 ? 1 : 0)
}

function buildZeroScale(values: Array<number | null>, targetTicks = 5) {
  const finite = values.filter(
    (value): value is number => typeof value === 'number' && Number.isFinite(value),
  )
  if (finite.length === 0) return null
  const min = Math.min(0, ...finite)
  const max = Math.max(0, ...finite)
  const span = max - min
  const step = niceStep(span > 0 ? span / Math.max(2, targetTicks - 1) : 1)
  const lower = Math.floor(min / step) * step
  const upper = Math.ceil(max / step) * step
  const ticks: number[] = []
  for (let tick = lower; tick <= upper + step / 2; tick += step) {
    ticks.push(Number(tick.toFixed(6)))
  }
  return { min: lower, max: upper, step, ticks }
}

function buildDeltas(values: Array<number | null>): Array<number | null> {
  let previous: number | null = null
  return values.map((value) => {
    if (typeof value !== 'number' || !Number.isFinite(value)) return null
    const delta = previous == null ? null : value - previous
    previous = value
    return delta
  })
}

function ShareChart({
  dates,
  series,
  emptyText,
  unitLabel,
  ariaLabel,
  labelByName = false,
  valueUnit = '',
  valueDigits = 2,
  sortTooltip = true,
}: {
  dates: string[]
  series: ChartSeries[]
  emptyText: string
  unitLabel: string
  ariaLabel: string
  labelByName?: boolean
  valueUnit?: string
  valueDigits?: number
  sortTooltip?: boolean
}) {
  const gradientId = useId()
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)

  const model = useMemo(() => {
    if (dates.length === 0 || series.length === 0) return null
    const scale = buildScale(series.flatMap((item) => item.values), 5)
    if (!scale) return null
    const innerWidth = CHART_WIDTH - CHART_PADDING.left - CHART_PADDING.right
    const innerHeight = CHART_HEIGHT - CHART_PADDING.top - CHART_PADDING.bottom
    const span = scale.max - scale.min || 1
    const bottom = CHART_PADDING.top + innerHeight
    const xOf = (index: number) =>
      dates.length === 1
        ? CHART_PADDING.left + innerWidth / 2
        : CHART_PADDING.left + (index / (dates.length - 1)) * innerWidth
    const yOf = (value: number) =>
      CHART_PADDING.top + innerHeight - ((value - scale.min) / span) * innerHeight
    const lines = series.map((item) => {
      const segments = buildSegments(item.values)
      const paths = segments.map((segment) =>
        segment
          .map((point, index) => {
            const command = index === 0 ? 'M' : 'L'
            return `${command}${xOf(point.index).toFixed(2)},${yOf(point.value).toFixed(2)}`
          })
          .join(' '),
      )
      const areaPaths = item.filled
        ? segments.map((segment, index) => {
            const first = segment[0]
            const last = segment[segment.length - 1]
            return `${paths[index]} L${xOf(last.index).toFixed(2)},${bottom.toFixed(2)} L${xOf(first.index).toFixed(2)},${bottom.toFixed(2)} Z`
          })
        : []
      const lastSegment = segments[segments.length - 1]
      const tailPoint = lastSegment ? lastSegment[lastSegment.length - 1] : null
      return {
        code: item.code,
        name: item.name,
        color: item.color,
        dashed: item.dashed,
        filled: item.filled,
        paths,
        areaPaths,
        tail: tailPoint ? { x: xOf(tailPoint.index), y: yOf(tailPoint.value) } : null,
      }
    })
    const xTicks = pickTickIndexes(dates.length, 7).map((index, position, all) => {
      const anchor: 'start' | 'middle' | 'end' =
        position === 0 ? 'start' : position === all.length - 1 ? 'end' : 'middle'
      return { x: xOf(index), label: dates[index], anchor }
    })
    return { scale, innerHeight, xOf, yOf, lines, xTicks }
  }, [dates, series])

  if (!model) {
    return (
      <div className="h-[220px] flex items-center justify-center text-[13px] text-muted-foreground">
        {emptyText}
      </div>
    )
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || dates.length === 0) return
    const svgX = ((event.clientX - rect.left) / rect.width) * CHART_WIDTH
    const innerWidth = CHART_WIDTH - CHART_PADDING.left - CHART_PADDING.right
    if (svgX < CHART_PADDING.left - 8 || svgX > CHART_PADDING.left + innerWidth + 8) {
      setHoverIndex(null)
      return
    }
    const ratio = Math.min(1, Math.max(0, (svgX - CHART_PADDING.left) / innerWidth))
    setHoverIndex(Math.round(ratio * (dates.length - 1)))
  }

  const hoverX = hoverIndex == null ? null : model.xOf(hoverIndex)
  const tooltipLeft = hoverX == null ? 0 : (hoverX / CHART_WIDTH) * 100
  const alignLeft = hoverX != null && hoverX > CHART_WIDTH * 0.62
  const hoveredValues: Array<{ code: string; name: string; color: string; value: number }> =
    hoverIndex == null
      ? []
      : series
          .map((item) => ({
            code: item.code,
            name: item.name,
            color: item.color,
            value: item.values[hoverIndex],
          }))
          .filter(
            (item): item is { code: string; name: string; color: string; value: number } =>
              typeof item.value === 'number' && Number.isFinite(item.value),
          )
  if (sortTooltip) {
    hoveredValues.sort((left, right) => right.value - left.value)
  }

  return (
    <div
      className="relative"
      onPointerMove={handlePointerMove}
      onPointerLeave={() => setHoverIndex(null)}
    >
      <svg
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        className="w-full h-auto select-none"
        role="img"
        aria-label={ariaLabel}
      >
        <defs>
          {model.lines
            .filter((line) => line.filled)
            .map((line) => (
              <linearGradient
                key={line.code}
                id={`${gradientId}-${line.code}`}
                x1="0"
                y1="0"
                x2="0"
                y2="1"
              >
                <stop offset="0%" stopColor={marketColorWithAlpha(line.color, 0.22)} />
                <stop offset="100%" stopColor={marketColorWithAlpha(line.color, 0.02)} />
              </linearGradient>
            ))}
        </defs>
        {model.scale.ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={CHART_PADDING.left}
              x2={CHART_WIDTH - CHART_PADDING.right}
              y1={model.yOf(tick)}
              y2={model.yOf(tick)}
              stroke="hsl(var(--border))"
              strokeWidth={0.5}
              strokeDasharray="2 4"
            />
            <text
              x={CHART_PADDING.left - 8}
              y={model.yOf(tick) + 3.5}
              textAnchor="end"
              fill="hsl(var(--muted-foreground))"
              fontSize={10}
            >
              {tickLabel(tick, model.scale.step)}
            </text>
          </g>
        ))}
        <text
          x={4}
          y={10}
          textAnchor="start"
          fill="hsl(var(--muted-foreground))"
          fontSize={9}
        >
          {unitLabel}
        </text>
        {model.lines.map((line) => (
          <g key={line.code}>
            {line.areaPaths.map((path, index) => (
              <path
                key={`${line.code}-area-${index}`}
                d={path}
                fill={`url(#${gradientId}-${line.code})`}
              />
            ))}
            {line.paths.map((path, index) => (
              <path
                key={`${line.code}-${index}`}
                d={path}
                fill="none"
                stroke={line.color}
                strokeWidth={line.dashed ? 1.4 : 1.8}
                strokeDasharray={line.dashed ? '5 4' : undefined}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ))}
            {line.tail && !line.dashed && (
              <circle cx={line.tail.x} cy={line.tail.y} r={2.4} fill={line.color} />
            )}
          </g>
        ))}
        {model.xTicks.map((tick) => (
          <text
            key={tick.label}
            x={tick.x}
            y={CHART_HEIGHT - 10}
            textAnchor={tick.anchor}
            fill="hsl(var(--muted-foreground))"
            fontSize={10}
          >
            {tick.label}
          </text>
        ))}
        {hoverIndex != null && hoverX != null && (
          <>
            <line
              x1={hoverX}
              x2={hoverX}
              y1={CHART_PADDING.top}
              y2={CHART_PADDING.top + model.innerHeight}
              stroke="hsl(var(--muted-foreground))"
              strokeWidth={0.8}
              strokeDasharray="3 3"
              opacity={0.6}
            />
            {hoveredValues.map((item) => (
              <circle
                key={item.code}
                cx={hoverX}
                cy={model.yOf(item.value)}
                r={2.8}
                fill={item.color}
                stroke="hsl(var(--background))"
                strokeWidth={1}
              />
            ))}
          </>
        )}
      </svg>
      {hoverIndex != null && hoveredValues.length > 0 && (
        <div
          className="pointer-events-none absolute top-1 z-10 min-w-[180px] max-w-[260px] rounded-md border border-border bg-popover px-2.5 py-2 shadow-sm"
          style={{
            left: `${tooltipLeft}%`,
            transform: alignLeft ? 'translateX(-100%)' : 'translateX(0)',
          }}
        >
          <div className="font-mono text-[11px] text-muted-foreground">{dates[hoverIndex]}</div>
          <div className="mt-1 space-y-0.5">
            {hoveredValues.map((item) => (
              <div key={item.code} className="flex items-center gap-1.5 text-[11px]">
                <span
                  className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ backgroundColor: item.color }}
                />
                <span className="font-mono text-muted-foreground">
                  {labelByName ? item.name : item.code}
                </span>
                <span className="ml-auto whitespace-nowrap font-mono font-medium text-foreground">
                  {formatValue(item.value, valueDigits)}
                  {valueUnit ? ` ${valueUnit}` : ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function DailyChangeChart({
  dates,
  values,
  emptyText,
  unitLabel,
  valueUnit,
  ariaLabel,
}: {
  dates: string[]
  values: Array<number | null>
  emptyText: string
  unitLabel: string
  valueUnit: string
  ariaLabel: string
}) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const deltas = useMemo(() => buildDeltas(values), [values])
  const model = useMemo(() => {
    if (dates.length === 0) return null
    const scale = buildZeroScale(deltas, 5)
    if (!scale) return null
    const innerWidth = DETAIL_WIDTH - DETAIL_PADDING.left - DETAIL_PADDING.right
    const innerHeight = DETAIL_HEIGHT - DETAIL_PADDING.top - DETAIL_PADDING.bottom
    const span = scale.max - scale.min || 1
    const slot = innerWidth / dates.length
    const barWidth = Math.max(1.5, Math.min(9, slot * 0.62))
    const xOf = (index: number) => DETAIL_PADDING.left + slot * (index + 0.5)
    const yOf = (value: number) =>
      DETAIL_PADDING.top + innerHeight - ((value - scale.min) / span) * innerHeight
    const yZero = yOf(0)
    const bars = deltas.flatMap((delta, index) => {
      if (typeof delta !== 'number' || !Number.isFinite(delta)) return []
      const y = yOf(delta)
      return [
        {
          index,
          x: xOf(index) - barWidth / 2,
          y: Math.min(y, yZero),
          height: Math.max(1, Math.abs(y - yZero)),
          width: barWidth,
          delta,
        },
      ]
    })
    const xTicks = pickTickIndexes(dates.length, 6).map((index, position, all) => {
      const anchor: 'start' | 'middle' | 'end' =
        position === 0 ? 'start' : position === all.length - 1 ? 'end' : 'middle'
      return { x: xOf(index), label: dates[index].slice(5), anchor }
    })
    return { scale, yOf, yZero, bars, xTicks, slot }
  }, [dates, deltas])

  if (!model) {
    return (
      <div className="h-[200px] flex items-center justify-center text-[13px] text-muted-foreground">
        {emptyText}
      </div>
    )
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || dates.length === 0) return
    const svgX = ((event.clientX - rect.left) / rect.width) * DETAIL_WIDTH
    const innerWidth = DETAIL_WIDTH - DETAIL_PADDING.left - DETAIL_PADDING.right
    if (svgX < DETAIL_PADDING.left - 8 || svgX > DETAIL_PADDING.left + innerWidth + 8) {
      setHoverIndex(null)
      return
    }
    const ratio = Math.min(1, Math.max(0, (svgX - DETAIL_PADDING.left) / innerWidth))
    setHoverIndex(Math.min(dates.length - 1, Math.floor(ratio * dates.length)))
  }

  const hoverDelta = hoverIndex == null ? null : deltas[hoverIndex]
  const hoverX =
    hoverIndex == null ? null : DETAIL_PADDING.left + model.slot * (hoverIndex + 0.5)
  const tooltipLeft = hoverX == null ? 0 : (hoverX / DETAIL_WIDTH) * 100
  const alignLeft = hoverX != null && hoverX > DETAIL_WIDTH * 0.62

  return (
    <div
      className="relative"
      onPointerMove={handlePointerMove}
      onPointerLeave={() => setHoverIndex(null)}
    >
      <svg
        viewBox={`0 0 ${DETAIL_WIDTH} ${DETAIL_HEIGHT}`}
        className="w-full h-auto select-none"
        role="img"
        aria-label={ariaLabel}
      >
        {model.scale.ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={DETAIL_PADDING.left}
              x2={DETAIL_WIDTH - DETAIL_PADDING.right}
              y1={model.yOf(tick)}
              y2={model.yOf(tick)}
              stroke="hsl(var(--border))"
              strokeWidth={0.5}
              strokeDasharray="2 4"
            />
            <text
              x={DETAIL_PADDING.left - 8}
              y={model.yOf(tick) + 3.5}
              textAnchor="end"
              fill="hsl(var(--muted-foreground))"
              fontSize={10}
            >
              {tickLabel(tick, model.scale.step)}
            </text>
          </g>
        ))}
        <line
          x1={DETAIL_PADDING.left}
          x2={DETAIL_WIDTH - DETAIL_PADDING.right}
          y1={model.yZero}
          y2={model.yZero}
          stroke="hsl(var(--border))"
          strokeWidth={0.9}
        />
        <text
          x={4}
          y={10}
          textAnchor="start"
          fill="hsl(var(--muted-foreground))"
          fontSize={9}
        >
          {unitLabel}
        </text>
        {model.bars.map((bar) => (
          <rect
            key={bar.index}
            x={bar.x}
            y={bar.y}
            width={bar.width}
            height={bar.height}
            rx={1}
            fill={
              bar.delta >= 0 ? 'hsl(var(--market-up))' : 'hsl(var(--market-down))'
            }
            opacity={hoverIndex == null || hoverIndex === bar.index ? 0.9 : 0.5}
          />
        ))}
        {model.xTicks.map((tick) => (
          <text
            key={tick.label}
            x={tick.x}
            y={DETAIL_HEIGHT - 10}
            textAnchor={tick.anchor}
            fill="hsl(var(--muted-foreground))"
            fontSize={10}
          >
            {tick.label}
          </text>
        ))}
        {hoverX != null && (
          <line
            x1={hoverX}
            x2={hoverX}
            y1={DETAIL_PADDING.top}
            y2={DETAIL_PADDING.top + (DETAIL_HEIGHT - DETAIL_PADDING.top - DETAIL_PADDING.bottom)}
            stroke="hsl(var(--muted-foreground))"
            strokeWidth={0.8}
            strokeDasharray="3 3"
            opacity={0.6}
          />
        )}
      </svg>
      {hoverIndex != null && (
        <div
          className="pointer-events-none absolute top-1 z-10 min-w-[140px] rounded-md border border-border bg-popover px-2.5 py-2 shadow-sm"
          style={{
            left: `${tooltipLeft}%`,
            transform: alignLeft ? 'translateX(-100%)' : 'translateX(0)',
          }}
        >
          <div className="font-mono text-[11px] text-muted-foreground">{dates[hoverIndex]}</div>
          <div className={`mt-1 font-mono text-[11px] font-medium ${marketSignTextClass(hoverDelta)}`}>
            {formatDelta(hoverDelta)}
            {valueUnit ? ` ${valueUnit}` : ''}
          </div>
        </div>
      )}
    </div>
  )
}

function RecentTrendChart({
  dates,
  values,
  color,
  emptyText,
  unitLabel,
  ariaLabel,
}: {
  dates: string[]
  values: Array<number | null>
  color: string
  emptyText: string
  unitLabel: string
  ariaLabel: string
}) {
  const gradientId = useId()
  const model = useMemo(() => {
    if (dates.length === 0) return null
    const scale = buildScale(values, 4)
    if (!scale) return null
    const innerWidth = DETAIL_WIDTH - DETAIL_PADDING.left - DETAIL_PADDING.right
    const innerHeight = DETAIL_HEIGHT - DETAIL_PADDING.top - DETAIL_PADDING.bottom
    const span = scale.max - scale.min || 1
    const xOf = (index: number) =>
      dates.length === 1
        ? DETAIL_PADDING.left + innerWidth / 2
        : DETAIL_PADDING.left + (index / (dates.length - 1)) * innerWidth
    const yOf = (value: number) =>
      DETAIL_PADDING.top + innerHeight - ((value - scale.min) / span) * innerHeight
    const bottom = DETAIL_PADDING.top + innerHeight
    const segments = buildSegments(values).map((segment) => {
      const line = segment
        .map((point, index) => {
          const command = index === 0 ? 'M' : 'L'
          return `${command}${xOf(point.index).toFixed(2)},${yOf(point.value).toFixed(2)}`
        })
        .join(' ')
      const first = segment[0]
      const last = segment[segment.length - 1]
      const area = `${line} L${xOf(last.index).toFixed(2)},${bottom.toFixed(2)} L${xOf(first.index).toFixed(2)},${bottom.toFixed(2)} Z`
      return { line, area }
    })
    const xTicks = pickTickIndexes(dates.length, 5).map((index, position, all) => {
      const anchor: 'start' | 'middle' | 'end' =
        position === 0 ? 'start' : position === all.length - 1 ? 'end' : 'middle'
      return { x: xOf(index), label: dates[index].slice(5), anchor }
    })
    return { scale, yOf, segments, xTicks }
  }, [dates, values])

  if (!model) {
    return (
      <div className="h-[200px] flex items-center justify-center text-[13px] text-muted-foreground">
        {emptyText}
      </div>
    )
  }

  return (
    <svg
      viewBox={`0 0 ${DETAIL_WIDTH} ${DETAIL_HEIGHT}`}
      className="w-full h-auto select-none"
      role="img"
      aria-label={ariaLabel}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={marketColorWithAlpha(color, 0.22)} />
          <stop offset="100%" stopColor={marketColorWithAlpha(color, 0.02)} />
        </linearGradient>
      </defs>
      {model.scale.ticks.map((tick) => (
        <g key={tick}>
          <line
            x1={DETAIL_PADDING.left}
            x2={DETAIL_WIDTH - DETAIL_PADDING.right}
            y1={model.yOf(tick)}
            y2={model.yOf(tick)}
            stroke="hsl(var(--border))"
            strokeWidth={0.5}
            strokeDasharray="2 4"
          />
          <text
            x={DETAIL_PADDING.left - 8}
            y={model.yOf(tick) + 3.5}
            textAnchor="end"
            fill="hsl(var(--muted-foreground))"
            fontSize={10}
          >
            {tickLabel(tick, model.scale.step)}
          </text>
        </g>
      ))}
      <text
        x={4}
        y={10}
        textAnchor="start"
        fill="hsl(var(--muted-foreground))"
        fontSize={9}
      >
        {unitLabel}
      </text>
      {model.segments.map((segment, index) => (
        <g key={index}>
          <path d={segment.area} fill={`url(#${gradientId})`} />
          <path
            d={segment.line}
            fill="none"
            stroke={color}
            strokeWidth={1.8}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </g>
      ))}
      {model.xTicks.map((tick) => (
        <text
          key={tick.label}
          x={tick.x}
          y={DETAIL_HEIGHT - 10}
          textAnchor={tick.anchor}
          fill="hsl(var(--muted-foreground))"
          fontSize={10}
        >
          {tick.label}
        </text>
      ))}
    </svg>
  )
}


function ChartLegend({
  items,
}: {
  items: Array<{ code: string; name: string; color: string; dashed?: boolean; bar?: boolean }>
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
      {items.map((item) => (
        <span key={item.code} className="flex items-center gap-1.5">
          {item.bar ? (
            <span
              className="h-2 w-2 shrink-0 rounded-sm"
              style={{ backgroundColor: item.color }}
            />
          ) : (
            <svg width="16" height="4" aria-hidden="true" className="shrink-0">
              <line
                x1="0"
                y1="2"
                x2="16"
                y2="2"
                stroke={item.color}
                strokeWidth="2"
                strokeDasharray={item.dashed ? '4 3' : undefined}
              />
            </svg>
          )}
          <span>{item.name}</span>
        </span>
      ))}
    </div>
  )
}

function MetricCard({
  metric,
  label,
  unit,
  color,
  digits = 2,
}: {
  metric: MarginMetric
  label: string
  unit: string
  color: string
  digits?: number
}) {
  return (
    <div className="relative overflow-hidden rounded-xl border border-border/60 bg-card/60 p-3">
      <span
        className="absolute inset-y-0 left-0 w-0.5"
        style={{ backgroundColor: color }}
      />
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <span className="h-2.5 w-1 rounded-sm" style={{ backgroundColor: color }} />
        <span className="truncate">{label}</span>
      </div>
      <div className="mt-1.5 flex items-baseline gap-1">
        <span className="font-mono text-[19px] font-bold leading-none text-foreground md:text-[21px]">
          {formatValue(metric.latest, digits)}
        </span>
        <span className="shrink-0 text-[10px] text-muted-foreground">{unit}</span>
      </div>
      <div className={`mt-1.5 font-mono text-[11px] ${marketSignTextClass(metric.delta)}`}>
        {formatDelta(metric.delta, digits)}
        <span className="ml-1">({formatPercent(metric.delta_pct)})</span>
      </div>
    </div>
  )
}

function MarginFlowChart({
  dates,
  buy,
  repay,
  buyLabel,
  repayLabel,
  buyColor,
  repayColor,
  emptyText,
  unitLabel,
  valueUnit,
  ariaLabel,
}: {
  dates: string[]
  buy: Array<number | null>
  repay: Array<number | null>
  buyLabel: string
  repayLabel: string
  buyColor: string
  repayColor: string
  emptyText: string
  unitLabel: string
  valueUnit: string
  ariaLabel: string
}) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const model = useMemo(() => {
    if (dates.length === 0) return null
    const scale = buildZeroScale([...buy, ...repay], 5)
    if (!scale) return null
    const innerWidth = DETAIL_WIDTH - DETAIL_PADDING.left - DETAIL_PADDING.right
    const innerHeight = DETAIL_HEIGHT - DETAIL_PADDING.top - DETAIL_PADDING.bottom
    const span = scale.max - scale.min || 1
    const slot = innerWidth / dates.length
    const barWidth = Math.max(1, Math.min(7, slot * 0.34))
    const gap = Math.max(0.6, Math.min(2.5, slot * 0.08))
    const xOf = (index: number) => DETAIL_PADDING.left + slot * (index + 0.5)
    const yOf = (value: number) =>
      DETAIL_PADDING.top + innerHeight - ((value - scale.min) / span) * innerHeight
    const yZero = yOf(0)
    const bars: Array<{
      index: number
      key: string
      x: number
      y: number
      height: number
      color: string
    }> = []
    dates.forEach((_, index) => {
      const buyValue = buy[index]
      const repayValue = repay[index]
      if (typeof buyValue === 'number' && Number.isFinite(buyValue)) {
        const y = yOf(buyValue)
        bars.push({
          index,
          key: `buy-${index}`,
          x: xOf(index) - gap / 2 - barWidth,
          y: Math.min(y, yZero),
          height: Math.max(0.8, Math.abs(y - yZero)),
          color: buyColor,
        })
      }
      if (typeof repayValue === 'number' && Number.isFinite(repayValue)) {
        const y = yOf(repayValue)
        bars.push({
          index,
          key: `repay-${index}`,
          x: xOf(index) + gap / 2,
          y: Math.min(y, yZero),
          height: Math.max(0.8, Math.abs(y - yZero)),
          color: repayColor,
        })
      }
    })
    const xTicks = pickTickIndexes(dates.length, 6).map((index, position, all) => {
      const anchor: 'start' | 'middle' | 'end' =
        position === 0 ? 'start' : position === all.length - 1 ? 'end' : 'middle'
      return { x: xOf(index), label: dates[index].slice(5), anchor }
    })
    return { scale, yOf, yZero, bars, xTicks, slot, barWidth }
  }, [dates, buy, repay, buyColor, repayColor])

  if (!model) {
    return (
      <div className="h-[200px] flex items-center justify-center text-[13px] text-muted-foreground">
        {emptyText}
      </div>
    )
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || dates.length === 0) return
    const svgX = ((event.clientX - rect.left) / rect.width) * DETAIL_WIDTH
    const innerWidth = DETAIL_WIDTH - DETAIL_PADDING.left - DETAIL_PADDING.right
    if (svgX < DETAIL_PADDING.left - 8 || svgX > DETAIL_PADDING.left + innerWidth + 8) {
      setHoverIndex(null)
      return
    }
    const ratio = Math.min(1, Math.max(0, (svgX - DETAIL_PADDING.left) / innerWidth))
    setHoverIndex(Math.min(dates.length - 1, Math.floor(ratio * dates.length)))
  }

  const hoverX =
    hoverIndex == null ? null : DETAIL_PADDING.left + model.slot * (hoverIndex + 0.5)
  const tooltipLeft = hoverX == null ? 0 : (hoverX / DETAIL_WIDTH) * 100
  const alignLeft = hoverX != null && hoverX > DETAIL_WIDTH * 0.62
  const hoverBuy = hoverIndex == null ? null : buy[hoverIndex]
  const hoverRepay = hoverIndex == null ? null : repay[hoverIndex]

  return (
    <div
      className="relative"
      onPointerMove={handlePointerMove}
      onPointerLeave={() => setHoverIndex(null)}
    >
      <svg
        viewBox={`0 0 ${DETAIL_WIDTH} ${DETAIL_HEIGHT}`}
        className="w-full h-auto select-none"
        role="img"
        aria-label={ariaLabel}
      >
        {model.scale.ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={DETAIL_PADDING.left}
              x2={DETAIL_WIDTH - DETAIL_PADDING.right}
              y1={model.yOf(tick)}
              y2={model.yOf(tick)}
              stroke="hsl(var(--border))"
              strokeWidth={0.5}
              strokeDasharray="2 4"
            />
            <text
              x={DETAIL_PADDING.left - 8}
              y={model.yOf(tick) + 3.5}
              textAnchor="end"
              fill="hsl(var(--muted-foreground))"
              fontSize={10}
            >
              {tickLabel(tick, model.scale.step)}
            </text>
          </g>
        ))}
        <line
          x1={DETAIL_PADDING.left}
          x2={DETAIL_WIDTH - DETAIL_PADDING.right}
          y1={model.yZero}
          y2={model.yZero}
          stroke="hsl(var(--border))"
          strokeWidth={0.9}
        />
        <text
          x={4}
          y={10}
          textAnchor="start"
          fill="hsl(var(--muted-foreground))"
          fontSize={9}
        >
          {unitLabel}
        </text>
        {model.bars.map((bar) => (
          <rect
            key={bar.key}
            x={bar.x}
            y={bar.y}
            width={model.barWidth}
            height={bar.height}
            rx={0.8}
            fill={bar.color}
            opacity={hoverIndex == null || hoverIndex === bar.index ? 0.9 : 0.45}
          />
        ))}
        {model.xTicks.map((tick) => (
          <text
            key={tick.label}
            x={tick.x}
            y={DETAIL_HEIGHT - 10}
            textAnchor={tick.anchor}
            fill="hsl(var(--muted-foreground))"
            fontSize={10}
          >
            {tick.label}
          </text>
        ))}
        {hoverX != null && (
          <line
            x1={hoverX}
            x2={hoverX}
            y1={DETAIL_PADDING.top}
            y2={DETAIL_PADDING.top + (DETAIL_HEIGHT - DETAIL_PADDING.top - DETAIL_PADDING.bottom)}
            stroke="hsl(var(--muted-foreground))"
            strokeWidth={0.8}
            strokeDasharray="3 3"
            opacity={0.6}
          />
        )}
      </svg>
      {hoverIndex != null && hoverX != null && (
        <div
          className="pointer-events-none absolute top-1 z-10 min-w-[160px] rounded-md border border-border bg-popover px-2.5 py-2 shadow-sm"
          style={{
            left: `${tooltipLeft}%`,
            transform: alignLeft ? 'translateX(-100%)' : 'translateX(0)',
          }}
        >
          <div className="font-mono text-[11px] text-muted-foreground">{dates[hoverIndex]}</div>
          <div className="mt-1 space-y-0.5">
            <div className="flex items-center gap-1.5 text-[11px]">
              <span
                className="h-2 w-2 shrink-0 rounded-sm"
                style={{ backgroundColor: buyColor }}
              />
              <span className="text-muted-foreground">{buyLabel}</span>
              <span className="ml-auto whitespace-nowrap font-mono font-medium text-foreground">
                {formatValue(hoverBuy)}
                {valueUnit ? ` ${valueUnit}` : ''}
              </span>
            </div>
            <div className="flex items-center gap-1.5 text-[11px]">
              <span
                className="h-2 w-2 shrink-0 rounded-sm"
                style={{ backgroundColor: repayColor }}
              />
              <span className="text-muted-foreground">{repayLabel}</span>
              <span className="ml-auto whitespace-nowrap font-mono font-medium text-foreground">
                {formatValue(hoverRepay)}
                {valueUnit ? ` ${valueUnit}` : ''}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function MarginDashboard({ data }: { data: MetricsPayload }) {
  const { t } = useTranslation('configuration')
  const amountUnit = t('etfShares.margin.unit')
  const volumeUnit = t('etfShares.margin.volumeUnit')
  const rzBalance = data.series.rz_balance ?? []
  const totalBalance = data.series.total_balance ?? []
  const rqVolume = data.series.rq_volume ?? []
  const rzBuy = data.series.rz_buy ?? []
  const rzRepay = data.series.rz_repay ?? []
  const rqMetric = data.metrics.find((metric) => metric.id === 'rq_volume') ?? null
  const balanceSeries: ChartSeries[] = [
    {
      code: 'rz_balance',
      name: t('etfShares.margin.charts.rzBalance'),
      color: MARGIN_METRIC_COLORS.rz_balance,
      values: rzBalance,
      filled: true,
    },
    {
      code: 'total_balance',
      name: t('etfShares.margin.charts.totalBalance'),
      color: MARGIN_METRIC_COLORS.total_balance,
      values: totalBalance,
      dashed: true,
    },
  ]

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 xl:grid-cols-6">
        {data.metrics.map((metric) => {
          const key = MARGIN_METRIC_KEYS[metric.id as keyof typeof MARGIN_METRIC_KEYS]
          return (
            <MetricCard
              key={metric.id}
              metric={metric}
              label={key ? t(key) : metric.id}
              unit={metric.id === 'rq_volume' ? volumeUnit : amountUnit}
              color={MARGIN_METRIC_COLORS[metric.id] ?? '#64748b'}
            />
          )
        })}
      </section>

      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
          <span
            className="h-4 w-1 rounded"
            style={{ backgroundColor: MARGIN_METRIC_COLORS.rz_balance }}
          />
          <h2 className="text-[13px] font-semibold">
            {t('etfShares.margin.charts.balanceTitle')}
          </h2>
          <div className="ml-auto">
            <ChartLegend
              items={balanceSeries.map((item) => ({
                code: item.code,
                name: item.name,
                color: item.color,
                dashed: item.dashed,
              }))}
            />
          </div>
        </div>
        <div className="px-2 pb-2 pt-3 md:px-4">
          <ShareChart
            dates={data.dates}
            series={balanceSeries}
            emptyText={t('etfShares.margin.charts.empty')}
            unitLabel={t('etfShares.margin.charts.balanceYUnit')}
            ariaLabel={t('etfShares.margin.charts.balanceTitle')}
            labelByName
            valueUnit={amountUnit}
            sortTooltip={false}
          />
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="card overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border/60 px-4 py-2.5">
            <span
              className="h-4 w-1 rounded"
              style={{ backgroundColor: MARGIN_METRIC_COLORS.rq_volume }}
            />
            <h2 className="text-[13px] font-semibold">
              {t('etfShares.margin.charts.rqTitle')}
            </h2>
            {rqMetric && (
              <span className="ml-auto font-mono text-[11px] text-muted-foreground">
                {formatValue(rqMetric.latest)}
                <span className="ml-1 font-sans">{volumeUnit}</span>
              </span>
            )}
          </div>
          <div className="px-2 pb-2 pt-3 md:px-4">
            <RecentTrendChart
              dates={data.dates}
              values={rqVolume}
              color={MARGIN_METRIC_COLORS.rq_volume}
              emptyText={t('etfShares.margin.charts.empty')}
              unitLabel={t('etfShares.margin.charts.rqYUnit')}
              ariaLabel={t('etfShares.margin.charts.rqTitle')}
            />
          </div>
        </section>

        <section className="card overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
            <span
              className="h-4 w-1 rounded"
              style={{ backgroundColor: MARGIN_METRIC_COLORS.rz_buy }}
            />
            <h2 className="text-[13px] font-semibold">
              {t('etfShares.margin.charts.flowTitle')}
            </h2>
            <div className="ml-auto">
              <ChartLegend
                items={[
                  {
                    code: 'rz_buy',
                    name: t('etfShares.margin.charts.flowBuy'),
                    color: MARGIN_METRIC_COLORS.rz_buy,
                    bar: true,
                  },
                  {
                    code: 'rz_repay',
                    name: t('etfShares.margin.charts.flowRepay'),
                    color: MARGIN_METRIC_COLORS.rz_repay,
                    bar: true,
                  },
                ]}
              />
            </div>
          </div>
          <div className="px-2 pb-2 pt-3 md:px-4">
            <MarginFlowChart
              dates={data.dates}
              buy={rzBuy}
              repay={rzRepay}
              buyLabel={t('etfShares.margin.charts.flowBuy')}
              repayLabel={t('etfShares.margin.charts.flowRepay')}
              buyColor={MARGIN_METRIC_COLORS.rz_buy}
              repayColor={MARGIN_METRIC_COLORS.rz_repay}
              emptyText={t('etfShares.margin.charts.empty')}
              unitLabel={t('etfShares.margin.charts.flowYUnit')}
              valueUnit={amountUnit}
              ariaLabel={t('etfShares.margin.charts.flowTitle')}
            />
          </div>
        </section>
      </div>

      <p className="px-1 text-[11px] text-muted-foreground/80">
        {t('etfShares.margin.caveat')}
      </p>
    </div>
  )
}

function PriceSpreadChart({
  dates,
  values,
  emptyText,
  unitLabel,
  valueUnit,
  valueDigits = 0,
  ariaLabel,
}: {
  dates: string[]
  values: Array<number | null>
  emptyText: string
  unitLabel: string
  valueUnit: string
  valueDigits?: number
  ariaLabel: string
}) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const model = useMemo(() => {
    if (dates.length === 0) return null
    const scale = buildZeroScale(values, 5)
    if (!scale) return null
    const innerWidth = DETAIL_WIDTH - DETAIL_PADDING.left - DETAIL_PADDING.right
    const innerHeight = DETAIL_HEIGHT - DETAIL_PADDING.top - DETAIL_PADDING.bottom
    const span = scale.max - scale.min || 1
    const slot = innerWidth / dates.length
    const barWidth = Math.max(1.5, Math.min(10, slot * 0.62))
    const xOf = (index: number) => DETAIL_PADDING.left + slot * (index + 0.5)
    const yOf = (value: number) =>
      DETAIL_PADDING.top + innerHeight - ((value - scale.min) / span) * innerHeight
    const yZero = yOf(0)
    const bars = values.flatMap((value, index) => {
      if (typeof value !== 'number' || !Number.isFinite(value)) return []
      const y = yOf(value)
      return [
        {
          index,
          x: xOf(index) - barWidth / 2,
          y: Math.min(y, yZero),
          height: Math.max(1, Math.abs(y - yZero)),
          width: barWidth,
          value,
        },
      ]
    })
    const xTicks = pickTickIndexes(dates.length, 6).map((index, position, all) => {
      const anchor: 'start' | 'middle' | 'end' =
        position === 0 ? 'start' : position === all.length - 1 ? 'end' : 'middle'
      return { x: xOf(index), label: dates[index].slice(5), anchor }
    })
    return { scale, yOf, yZero, bars, xTicks, slot }
  }, [dates, values])

  if (!model) {
    return (
      <div className="h-[200px] flex items-center justify-center text-[13px] text-muted-foreground">
        {emptyText}
      </div>
    )
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || dates.length === 0) return
    const svgX = ((event.clientX - rect.left) / rect.width) * DETAIL_WIDTH
    const innerWidth = DETAIL_WIDTH - DETAIL_PADDING.left - DETAIL_PADDING.right
    if (svgX < DETAIL_PADDING.left - 8 || svgX > DETAIL_PADDING.left + innerWidth + 8) {
      setHoverIndex(null)
      return
    }
    const ratio = Math.min(1, Math.max(0, (svgX - DETAIL_PADDING.left) / innerWidth))
    setHoverIndex(Math.min(dates.length - 1, Math.floor(ratio * dates.length)))
  }

  const hoverValue = hoverIndex == null ? null : values[hoverIndex]
  const hoverX =
    hoverIndex == null ? null : DETAIL_PADDING.left + model.slot * (hoverIndex + 0.5)
  const tooltipLeft = hoverX == null ? 0 : (hoverX / DETAIL_WIDTH) * 100
  const alignLeft = hoverX != null && hoverX > DETAIL_WIDTH * 0.62

  return (
    <div
      className="relative"
      onPointerMove={handlePointerMove}
      onPointerLeave={() => setHoverIndex(null)}
    >
      <svg
        viewBox={`0 0 ${DETAIL_WIDTH} ${DETAIL_HEIGHT}`}
        className="w-full h-auto select-none"
        role="img"
        aria-label={ariaLabel}
      >
        {model.scale.ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={DETAIL_PADDING.left}
              x2={DETAIL_WIDTH - DETAIL_PADDING.right}
              y1={model.yOf(tick)}
              y2={model.yOf(tick)}
              stroke="hsl(var(--border))"
              strokeWidth={0.5}
              strokeDasharray="2 4"
            />
            <text
              x={DETAIL_PADDING.left - 8}
              y={model.yOf(tick) + 3.5}
              textAnchor="end"
              fill="hsl(var(--muted-foreground))"
              fontSize={10}
            >
              {tickLabel(tick, model.scale.step)}
            </text>
          </g>
        ))}
        <line
          x1={DETAIL_PADDING.left}
          x2={DETAIL_WIDTH - DETAIL_PADDING.right}
          y1={model.yZero}
          y2={model.yZero}
          stroke="hsl(var(--border))"
          strokeWidth={0.9}
        />
        <text
          x={4}
          y={10}
          textAnchor="start"
          fill="hsl(var(--muted-foreground))"
          fontSize={9}
        >
          {unitLabel}
        </text>
        {model.bars.map((bar) => (
          <rect
            key={bar.index}
            x={bar.x}
            y={bar.y}
            width={bar.width}
            height={bar.height}
            rx={1}
            fill={
              bar.value >= 0 ? 'hsl(var(--market-up))' : 'hsl(var(--market-down))'
            }
            opacity={hoverIndex == null || hoverIndex === bar.index ? 0.9 : 0.5}
          />
        ))}
        {model.xTicks.map((tick) => (
          <text
            key={`${tick.label}-${tick.x}`}
            x={tick.x}
            y={DETAIL_HEIGHT - 10}
            textAnchor={tick.anchor}
            fill="hsl(var(--muted-foreground))"
            fontSize={10}
          >
            {tick.label}
          </text>
        ))}
        {hoverX != null && (
          <line
            x1={hoverX}
            x2={hoverX}
            y1={DETAIL_PADDING.top}
            y2={DETAIL_HEIGHT - DETAIL_PADDING.bottom}
            stroke="hsl(var(--muted-foreground))"
            strokeWidth={0.8}
            strokeDasharray="3 3"
            opacity={0.6}
          />
        )}
      </svg>
      {hoverIndex != null && (
        <div
          className="pointer-events-none absolute top-1 z-10 min-w-[150px] rounded-md border border-border bg-popover px-2.5 py-2 shadow-sm"
          style={{
            left: `${tooltipLeft}%`,
            transform: alignLeft ? 'translateX(-100%)' : 'translateX(0)',
          }}
        >
          <div className="font-mono text-[11px] text-muted-foreground">{dates[hoverIndex]}</div>
          <div className={`mt-1 font-mono text-[11px] font-medium ${marketSignTextClass(hoverValue)}`}>
            {formatValue(hoverValue, valueDigits)}
            {valueUnit ? ` ${valueUnit}` : ''}
          </div>
        </div>
      )}
    </div>
  )
}

const DUAL_PADDING = { top: 26, right: 58, bottom: 30, left: 58 }

function DualAxisChart({
  dates,
  leftValues,
  rightValues,
  leftLabel,
  rightLabel,
  leftColor,
  rightColor,
  emptyText,
  valueUnit,
  ariaLabel,
}: {
  dates: string[]
  leftValues: Array<number | null>
  rightValues: Array<number | null>
  leftLabel: string
  rightLabel: string
  leftColor: string
  rightColor: string
  emptyText: string
  valueUnit: string
  ariaLabel: string
}) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const model = useMemo(() => {
    if (dates.length === 0) return null
    const leftScale = buildScale(leftValues, 5)
    const rightScale = buildScale(rightValues, 5)
    if (!leftScale || !rightScale) return null
    const innerWidth = CHART_WIDTH - DUAL_PADDING.left - DUAL_PADDING.right
    const innerHeight = CHART_HEIGHT - DUAL_PADDING.top - DUAL_PADDING.bottom
    const leftSpan = leftScale.max - leftScale.min || 1
    const rightSpan = rightScale.max - rightScale.min || 1
    const xOf = (index: number) =>
      dates.length === 1
        ? DUAL_PADDING.left + innerWidth / 2
        : DUAL_PADDING.left + (index / (dates.length - 1)) * innerWidth
    const yLeft = (value: number) =>
      DUAL_PADDING.top + innerHeight - ((value - leftScale.min) / leftSpan) * innerHeight
    const yRight = (value: number) =>
      DUAL_PADDING.top + innerHeight - ((value - rightScale.min) / rightSpan) * innerHeight
    const pathFor = (
      segments: Array<Array<{ index: number; value: number }>>,
      yOf: (value: number) => number,
    ) =>
      segments.map((segment) =>
        segment
          .map((point, index) => {
            const command = index === 0 ? 'M' : 'L'
            return `${command}${xOf(point.index).toFixed(2)},${yOf(point.value).toFixed(2)}`
          })
          .join(' '),
      )
    const leftPaths = pathFor(buildSegments(leftValues), yLeft)
    const rightPaths = pathFor(buildSegments(rightValues), yRight)
    const xTicks = pickTickIndexes(dates.length, 7).map((index, position, all) => {
      const anchor: 'start' | 'middle' | 'end' =
        position === 0 ? 'start' : position === all.length - 1 ? 'end' : 'middle'
      return { x: xOf(index), label: dates[index].slice(5), anchor }
    })
    return {
      leftScale,
      rightScale,
      yLeft,
      yRight,
      xOf,
      leftPaths,
      rightPaths,
      xTicks,
    }
  }, [dates, leftValues, rightValues])

  if (!model) {
    return (
      <div className="h-[220px] flex items-center justify-center text-[13px] text-muted-foreground">
        {emptyText}
      </div>
    )
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || dates.length === 0) return
    const svgX = ((event.clientX - rect.left) / rect.width) * CHART_WIDTH
    const innerWidth = CHART_WIDTH - DUAL_PADDING.left - DUAL_PADDING.right
    if (svgX < DUAL_PADDING.left - 8 || svgX > DUAL_PADDING.left + innerWidth + 8) {
      setHoverIndex(null)
      return
    }
    const ratio = Math.min(1, Math.max(0, (svgX - DUAL_PADDING.left) / innerWidth))
    setHoverIndex(Math.round(ratio * (dates.length - 1)))
  }

  const hoverX = hoverIndex == null ? null : model.xOf(hoverIndex)
  const tooltipLeft = hoverX == null ? 0 : (hoverX / CHART_WIDTH) * 100
  const alignLeft = hoverX != null && hoverX > CHART_WIDTH * 0.62
  const leftHover = hoverIndex == null ? null : leftValues[hoverIndex]
  const rightHover = hoverIndex == null ? null : rightValues[hoverIndex]

  return (
    <div
      className="relative"
      onPointerMove={handlePointerMove}
      onPointerLeave={() => setHoverIndex(null)}
    >
      <svg
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        className="w-full h-auto select-none"
        role="img"
        aria-label={ariaLabel}
      >
        {model.leftScale.ticks.map((tick) => (
          <g key={`left-${tick}`}>
            <line
              x1={DUAL_PADDING.left}
              x2={CHART_WIDTH - DUAL_PADDING.right}
              y1={model.yLeft(tick)}
              y2={model.yLeft(tick)}
              stroke="hsl(var(--border))"
              strokeWidth={0.5}
              strokeDasharray="2 4"
            />
            <text
              x={DUAL_PADDING.left - 8}
              y={model.yLeft(tick) + 3.5}
              textAnchor="end"
              fill="hsl(var(--muted-foreground))"
              fontSize={10}
            >
              {tickLabel(tick, model.leftScale.step)}
            </text>
          </g>
        ))}
        {model.rightScale.ticks.map((tick) => (
          <text
            key={`right-${tick}`}
            x={CHART_WIDTH - DUAL_PADDING.right + 8}
            y={model.yRight(tick) + 3.5}
            textAnchor="start"
            fill="hsl(var(--muted-foreground))"
            fontSize={10}
          >
            {tickLabel(tick, model.rightScale.step)}
          </text>
        ))}
        <text
          x={4}
          y={12}
          textAnchor="start"
          fill={leftColor}
          fontSize={9}
        >
          {leftLabel}
        </text>
        <text
          x={CHART_WIDTH - 4}
          y={12}
          textAnchor="end"
          fill={rightColor}
          fontSize={9}
        >
          {rightLabel}
        </text>
        {model.leftPaths.map((path, index) => (
          <path
            key={`left-path-${index}`}
            d={path}
            fill="none"
            stroke={leftColor}
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))}
        {model.rightPaths.map((path, index) => (
          <path
            key={`right-path-${index}`}
            d={path}
            fill="none"
            stroke={rightColor}
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))}
        {model.xTicks.map((tick) => (
          <text
            key={`${tick.label}-${tick.x}`}
            x={tick.x}
            y={CHART_HEIGHT - 10}
            textAnchor={tick.anchor}
            fill="hsl(var(--muted-foreground))"
            fontSize={10}
          >
            {tick.label}
          </text>
        ))}
        {hoverX != null && (
          <line
            x1={hoverX}
            x2={hoverX}
            y1={DUAL_PADDING.top}
            y2={CHART_HEIGHT - DUAL_PADDING.bottom}
            stroke="hsl(var(--muted-foreground))"
            strokeWidth={0.8}
            strokeDasharray="3 3"
            opacity={0.6}
          />
        )}
      </svg>
      {hoverIndex != null && (
        <div
          className="pointer-events-none absolute top-1 z-10 min-w-[190px] rounded-md border border-border bg-popover px-2.5 py-2 shadow-sm"
          style={{
            left: `${tooltipLeft}%`,
            transform: alignLeft ? 'translateX(-100%)' : 'translateX(0)',
          }}
        >
          <div className="font-mono text-[11px] text-muted-foreground">{dates[hoverIndex]}</div>
          <div className="mt-1 space-y-0.5">
            <div className="flex items-center gap-1.5 text-[11px]">
              <span
                className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ backgroundColor: leftColor }}
              />
              <span className="text-muted-foreground">{leftLabel}</span>
              <span className="ml-auto whitespace-nowrap font-mono font-medium text-foreground">
                {formatValue(leftHover, 0)}
                {valueUnit ? ` ${valueUnit}` : ''}
              </span>
            </div>
            <div className="flex items-center gap-1.5 text-[11px]">
              <span
                className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ backgroundColor: rightColor }}
              />
              <span className="text-muted-foreground">{rightLabel}</span>
              <span className="ml-auto whitespace-nowrap font-mono font-medium text-foreground">
                {formatValue(rightHover, 0)}
                {valueUnit ? ` ${valueUnit}` : ''}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function AluminumDashboard({ data }: { data: MetricsPayload }) {
  const { t } = useTranslation('configuration')
  const unit = t('etfShares.aluminum.unit')
  const a00Avg = data.series.a00_avg ?? []
  const aluminaAvg = data.series.alumina_avg ?? []
  const spread = data.series.spread ?? []
  const overlay = data.overlay
  const aluminaSeries: ChartSeries[] = [
    {
      code: 'alumina_avg',
      name: t('etfShares.aluminum.legend.aluminaAverage'),
      color: ALUMINUM_METRIC_COLORS.alumina_avg,
      values: aluminaAvg,
      filled: true,
    },
  ]
  const recentCount = Math.min(20, data.dates.length)
  const recentDates = data.dates.slice(-recentCount)
  const recentA00 = a00Avg.slice(-recentCount)
  const recentAlumina = aluminaAvg.slice(-recentCount)

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
        {data.metrics.map((metric) => {
          const key = ALUMINUM_METRIC_KEYS[metric.id as keyof typeof ALUMINUM_METRIC_KEYS]
          return (
            <MetricCard
              key={metric.id}
              metric={metric}
              label={key ? t(key) : metric.id}
              unit={unit}
              color={ALUMINUM_METRIC_COLORS[metric.id] ?? '#64748b'}
              digits={0}
            />
          )
        })}
      </section>

      {overlay && overlay.dates.length > 0 && (
        <section className="card overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
            <span
              className="h-4 w-1 rounded"
              style={{ backgroundColor: ALUMINUM_A00_COLORS.avg }}
            />
            <h2 className="text-[13px] font-semibold">
              {t('etfShares.aluminum.charts.overlayTitle')}
            </h2>
            <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {t('etfShares.aluminum.charts.daily')}
            </span>
            <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {t('etfShares.aluminum.charts.overlay')}
            </span>
            {data.as_of && (
              <span className="ml-auto font-mono text-[11px] text-muted-foreground">
                {data.as_of}
              </span>
            )}
          </div>
          <div className="px-2 pb-2 pt-3 md:px-4">
            <p className="mb-2 px-1 text-[11px] text-muted-foreground/80">
              {t('etfShares.aluminum.charts.overlayHint')}
            </p>
            <YearOverlayChart
              dates={overlay.dates}
              values={overlay.values}
              emptyText={t('etfShares.aluminum.charts.empty')}
              unitLabel={t('etfShares.aluminum.charts.a00YUnit')}
              valueUnit={unit}
              ariaLabel={t('etfShares.aluminum.charts.overlayAria')}
            />
          </div>
        </section>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="card overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
            <span
              className="h-4 w-1 rounded"
              style={{ backgroundColor: ALUMINUM_METRIC_COLORS.alumina_avg }}
            />
            <h2 className="text-[13px] font-semibold">
              {t('etfShares.aluminum.charts.aluminaTitle')}
            </h2>
            <div className="ml-auto">
              <ChartLegend
                items={[
                  {
                    code: 'alumina_avg',
                    name: t('etfShares.aluminum.legend.aluminaAverage'),
                    color: ALUMINUM_METRIC_COLORS.alumina_avg,
                  },
                ]}
              />
            </div>
          </div>
          <div className="px-2 pb-2 pt-3 md:px-4">
            <p className="mb-1 text-[11px] text-muted-foreground">
              {t('etfShares.aluminum.charts.aluminaHint')}
            </p>
            <ShareChart
              dates={data.dates}
              series={aluminaSeries}
              emptyText={t('etfShares.aluminum.charts.empty')}
              unitLabel={t('etfShares.aluminum.charts.aluminaYUnit')}
              ariaLabel={t('etfShares.aluminum.charts.aluminaTitle')}
              labelByName
              valueUnit={unit}
              valueDigits={0}
            />
          </div>
        </section>

        <section className="card overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
            <span
              className="h-4 w-1 rounded"
              style={{ backgroundColor: ALUMINUM_METRIC_COLORS.spread }}
            />
            <h2 className="text-[13px] font-semibold">
              {t('etfShares.aluminum.charts.spreadTitle')}
            </h2>
            <span className="ml-auto text-[11px] text-muted-foreground">
              {t('etfShares.aluminum.charts.spreadHint')}
            </span>
          </div>
          <div className="px-2 pb-2 pt-3 md:px-4">
            <PriceSpreadChart
              dates={data.dates}
              values={spread}
              emptyText={t('etfShares.aluminum.charts.empty')}
              unitLabel={t('etfShares.aluminum.charts.spreadYUnit')}
              valueUnit={unit}
              ariaLabel={t('etfShares.aluminum.charts.spreadTitle')}
            />
          </div>
        </section>
      </div>

      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
          <span
            className="h-4 w-1 rounded"
            style={{ backgroundColor: ALUMINUM_A00_COLORS.avg }}
          />
          <h2 className="text-[13px] font-semibold">
            {t('etfShares.aluminum.charts.compareTitle')}
          </h2>
          <span className="text-[11px] text-muted-foreground">
            {t('etfShares.aluminum.charts.compareHint')}
          </span>
          <div className="ml-auto">
            <ChartLegend
              items={[
                {
                  code: 'a00',
                  name: t('etfShares.aluminum.legend.a00'),
                  color: ALUMINUM_A00_COLORS.avg,
                },
                {
                  code: 'alumina',
                  name: t('etfShares.aluminum.legend.alumina'),
                  color: ALUMINUM_METRIC_COLORS.alumina_avg,
                },
              ]}
            />
          </div>
        </div>
        <div className="px-2 pb-2 pt-3 md:px-4">
          <DualAxisChart
            dates={recentDates}
            leftValues={recentA00}
            rightValues={recentAlumina}
            leftLabel={t('etfShares.aluminum.legend.a00')}
            rightLabel={t('etfShares.aluminum.legend.alumina')}
            leftColor={ALUMINUM_A00_COLORS.avg}
            rightColor={ALUMINUM_METRIC_COLORS.alumina_avg}
            emptyText={t('etfShares.aluminum.charts.empty')}
            valueUnit={unit}
            ariaLabel={t('etfShares.aluminum.charts.compareTitle')}
          />
        </div>
      </section>

      <p className="px-1 text-[11px] text-muted-foreground/80">
        {t('etfShares.aluminum.caveat')}
      </p>
    </div>
  )
}

function AluminumInventoryDashboard({ data }: { data: MetricsPayload }) {
  const { t } = useTranslation('configuration')
  const unit = t('etfShares.aluminumInventory.unit')
  const social = data.series.social ?? []
  const factory = data.series.factory ?? []
  const socialChange = data.series.social_change ?? []
  const factoryDates: string[] = []
  const factoryValues: Array<number | null> = []
  const socialDates: string[] = []
  const socialValues: Array<number | null> = []
  const socialChanges: Array<number | null> = []
  data.dates.forEach((day, index) => {
    const factoryValue = factory[index]
    if (typeof factoryValue === 'number') {
      factoryDates.push(day)
      factoryValues.push(factoryValue)
    }
    const socialValue = social[index]
    if (typeof socialValue === 'number') {
      socialDates.push(day)
      socialValues.push(socialValue)
      socialChanges.push(socialChange[index] ?? null)
    }
  })
  const socialSeries: ChartSeries[] = [
    {
      code: 'social',
      name: t('etfShares.aluminumInventory.metrics.social'),
      color: ALUMINUM_INVENTORY_METRIC_COLORS.social,
      values: socialValues,
      filled: true,
    },
  ]
  const factorySeries: ChartSeries[] = [
    {
      code: 'factory',
      name: t('etfShares.aluminumInventory.metrics.factory'),
      color: ALUMINUM_INVENTORY_METRIC_COLORS.factory,
      values: factoryValues,
      filled: true,
    },
  ]

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
        {data.metrics.map((metric) => {
          const key =
            ALUMINUM_INVENTORY_METRIC_KEYS[
              metric.id as keyof typeof ALUMINUM_INVENTORY_METRIC_KEYS
            ]
          return (
            <MetricCard
              key={metric.id}
              metric={metric}
              label={key ? t(key) : metric.id}
              unit={unit}
              color={ALUMINUM_INVENTORY_METRIC_COLORS[metric.id] ?? '#64748b'}
              digits={1}
            />
          )
        })}
      </section>

      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
          <span
            className="h-4 w-1 rounded"
            style={{ backgroundColor: ALUMINUM_INVENTORY_METRIC_COLORS.social }}
          />
          <h2 className="text-[13px] font-semibold">
            {t('etfShares.aluminumInventory.charts.socialTitle')}
          </h2>
          <span className="text-[11px] text-muted-foreground">
            {t('etfShares.aluminumInventory.charts.socialHint')}
          </span>
          <div className="ml-auto">
            <ChartLegend
              items={[
                {
                  code: 'social',
                  name: t('etfShares.aluminumInventory.metrics.social'),
                  color: ALUMINUM_INVENTORY_METRIC_COLORS.social,
                },
              ]}
            />
          </div>
        </div>
        <div className="px-2 pb-2 pt-3 md:px-4">
          <ShareChart
            dates={socialDates}
            series={socialSeries}
            emptyText={t('etfShares.aluminumInventory.charts.empty')}
            unitLabel={t('etfShares.aluminumInventory.charts.socialYUnit')}
            ariaLabel={t('etfShares.aluminumInventory.charts.socialTitle')}
            labelByName
            valueUnit={unit}
            valueDigits={1}
            sortTooltip={false}
          />
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="card overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
            <span
              className="h-4 w-1 rounded"
              style={{ backgroundColor: ALUMINUM_INVENTORY_METRIC_COLORS.factory }}
            />
            <h2 className="text-[13px] font-semibold">
              {t('etfShares.aluminumInventory.charts.factoryTitle')}
            </h2>
            <span className="ml-auto text-[11px] text-muted-foreground">
              {t('etfShares.aluminumInventory.charts.factoryHint')}
            </span>
          </div>
          <div className="px-2 pb-2 pt-3 md:px-4">
            <ShareChart
              dates={factoryDates}
              series={factorySeries}
              emptyText={t('etfShares.aluminumInventory.charts.empty')}
              unitLabel={t('etfShares.aluminumInventory.charts.factoryYUnit')}
              ariaLabel={t('etfShares.aluminumInventory.charts.factoryTitle')}
              labelByName
              valueUnit={unit}
              valueDigits={1}
            />
          </div>
        </section>

        <section className="card overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
            <span
              className="h-4 w-1 rounded"
              style={{ backgroundColor: ALUMINUM_INVENTORY_METRIC_COLORS.total }}
            />
            <h2 className="text-[13px] font-semibold">
              {t('etfShares.aluminumInventory.charts.changeTitle')}
            </h2>
            <span className="ml-auto text-[11px] text-muted-foreground">
              {t('etfShares.aluminumInventory.charts.changeHint')}
            </span>
          </div>
          <div className="px-2 pb-2 pt-3 md:px-4">
            <PriceSpreadChart
              dates={socialDates}
              values={socialChanges}
              emptyText={t('etfShares.aluminumInventory.charts.empty')}
              unitLabel={t('etfShares.aluminumInventory.charts.changeYUnit')}
              valueUnit={unit}
              ariaLabel={t('etfShares.aluminumInventory.charts.changeTitle')}
            />
          </div>
        </section>
      </div>

      <p className="px-1 text-[11px] text-muted-foreground/80">
        {t('etfShares.aluminumInventory.caveat')}
      </p>
    </div>
  )
}

function ThreeGorgesDashboard({ data }: { data: MetricsPayload }) {
  const { t } = useTranslation('configuration')
  const level = data.series.level ?? []
  const inflow = data.series.inflow ?? []
  const outflow = data.series.outflow ?? []
  const levelSeries: ChartSeries[] = [
    {
      code: 'level',
      name: t('etfShares.threeGorges.metrics.level'),
      color: THREE_GORGES_METRIC_COLORS.level,
      values: level,
      filled: true,
    },
  ]
  const flowSeries: ChartSeries[] = [
    {
      code: 'inflow',
      name: t('etfShares.threeGorges.metrics.inflow'),
      color: THREE_GORGES_METRIC_COLORS.inflow,
      values: inflow,
    },
    {
      code: 'outflow',
      name: t('etfShares.threeGorges.metrics.outflow'),
      color: THREE_GORGES_METRIC_COLORS.outflow,
      values: outflow,
    },
  ]

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
        {data.metrics.map((metric) => {
          const key =
            THREE_GORGES_METRIC_KEYS[
              metric.id as keyof typeof THREE_GORGES_METRIC_KEYS
            ]
          const unitKey =
            THREE_GORGES_METRIC_UNITS[
              metric.id as keyof typeof THREE_GORGES_METRIC_UNITS
            ]
          const digits = metric.id === 'level' ? 2 : 0
          return (
            <MetricCard
              key={metric.id}
              metric={metric}
              label={key ? t(key) : metric.id}
              unit={unitKey ? t(unitKey) : ''}
              color={THREE_GORGES_METRIC_COLORS[metric.id] ?? '#64748b'}
              digits={digits}
            />
          )
        })}
      </section>

      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
          <span
            className="h-4 w-1 rounded"
            style={{ backgroundColor: THREE_GORGES_METRIC_COLORS.level }}
          />
          <h2 className="text-[13px] font-semibold">
            {t('etfShares.threeGorges.charts.levelTitle')}
          </h2>
          <span className="text-[11px] text-muted-foreground">
            {t('etfShares.threeGorges.charts.levelHint')}
          </span>
          <div className="ml-auto">
            <ChartLegend
              items={[
                {
                  code: 'level',
                  name: t('etfShares.threeGorges.metrics.level'),
                  color: THREE_GORGES_METRIC_COLORS.level,
                },
              ]}
            />
          </div>
        </div>
        <div className="px-2 pb-2 pt-3 md:px-4">
          <ShareChart
            dates={data.dates}
            series={levelSeries}
            emptyText={t('etfShares.threeGorges.charts.empty')}
            unitLabel={t('etfShares.threeGorges.charts.levelYUnit')}
            ariaLabel={t('etfShares.threeGorges.charts.levelTitle')}
            labelByName
            valueUnit={t('etfShares.threeGorges.units.level')}
            valueDigits={2}
            sortTooltip={false}
          />
        </div>
      </section>

      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
          <span
            className="h-4 w-1 rounded"
            style={{ backgroundColor: THREE_GORGES_METRIC_COLORS.inflow }}
          />
          <h2 className="text-[13px] font-semibold">
            {t('etfShares.threeGorges.charts.flowTitle')}
          </h2>
          <span className="text-[11px] text-muted-foreground">
            {t('etfShares.threeGorges.charts.flowHint')}
          </span>
          <div className="ml-auto">
            <ChartLegend
              items={flowSeries.map((item) => ({
                code: item.code,
                name: item.name,
                color: item.color,
              }))}
            />
          </div>
        </div>
        <div className="px-2 pb-2 pt-3 md:px-4">
          <ShareChart
            dates={data.dates}
            series={flowSeries}
            emptyText={t('etfShares.threeGorges.charts.empty')}
            unitLabel={t('etfShares.threeGorges.charts.flowYUnit')}
            ariaLabel={t('etfShares.threeGorges.charts.flowTitle')}
            labelByName
            valueUnit={t('etfShares.threeGorges.units.flow')}
            valueDigits={0}
          />
        </div>
      </section>

      <p className="px-1 text-[11px] text-muted-foreground/80">
        {t('etfShares.threeGorges.caveat')}
      </p>
    </div>
  )
}

type CoalEditor = { mode: 'create' } | { mode: 'edit'; original: string }

interface YearOverlayPoint {
  day: string
  value: number
  axisIndex: number
  x: number
  y: number
}

function yearOverlayColor(index: number, total: number): string {
  if (total <= 1) return COAL_METRIC_COLORS.cci_5500
  const latest = YEAR_COLORS[YEAR_COLORS.length - 1]
  const offset = Math.max(0, YEAR_COLORS.length - total)
  if (offset === 0) {
    return index === total - 1 ? latest : YEAR_COLORS[index % (YEAR_COLORS.length - 1)]
  }
  return YEAR_COLORS[Math.min(YEAR_COLORS.length - 1, offset + index)]
}

function YearOverlayChart({
  dates,
  values,
  emptyText,
  unitLabel,
  valueUnit,
  ariaLabel,
}: {
  dates: string[]
  values: Array<number | null>
  emptyText: string
  unitLabel: string
  valueUnit: string
  ariaLabel: string
}) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const model = useMemo(() => {
    const byYear = new Map<string, Array<{ day: string; value: number }>>()
    const axisSet = new Set<string>()
    dates.forEach((date, index) => {
      const value = values[index]
      if (typeof value !== 'number' || !Number.isFinite(value)) return
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return
      const year = date.slice(0, 4)
      const day = date.slice(5)
      axisSet.add(day)
      const rows = byYear.get(year)
      if (rows) rows.push({ day, value })
      else byYear.set(year, [{ day, value }])
    })
    if (axisSet.size === 0 || byYear.size === 0) return null
    const axis = [...axisSet].sort()
    const scale = buildScale(
      [...byYear.values()].flatMap((rows) => rows.map((row) => row.value)),
      5,
    )
    if (!scale) return null
    const innerWidth = CHART_WIDTH - CHART_PADDING.left - CHART_PADDING.right
    const innerHeight = CHART_HEIGHT - CHART_PADDING.top - CHART_PADDING.bottom
    const span = scale.max - scale.min || 1
    const xOf = (index: number) =>
      axis.length === 1
        ? CHART_PADDING.left + innerWidth / 2
        : CHART_PADDING.left + (index / (axis.length - 1)) * innerWidth
    const yOf = (value: number) =>
      CHART_PADDING.top + innerHeight - ((value - scale.min) / span) * innerHeight
    const indexByDay = new Map(axis.map((day, index) => [day, index]))
    const years = [...byYear.keys()].sort()
    const lines = years.map((year, lineIndex) => {
      const points: YearOverlayPoint[] = (byYear.get(year) ?? [])
        .sort((left, right) => left.day.localeCompare(right.day))
        .map((row) => {
          const axisIndex = indexByDay.get(row.day) ?? 0
          return {
            ...row,
            axisIndex,
            x: xOf(axisIndex),
            y: yOf(row.value),
          }
        })
      const path = points
        .map((point, pointIndex) => {
          const command = pointIndex === 0 ? 'M' : 'L'
          return `${command}${point.x.toFixed(2)},${point.y.toFixed(2)}`
        })
        .join(' ')
      return {
        year,
        color: yearOverlayColor(lineIndex, years.length),
        path,
        points,
        tail: points.length > 0 ? points[points.length - 1] : null,
      }
    })
    const tickIndexes = new Set<number>()
    ;['01', '03', '05', '07', '09', '11'].forEach((month) => {
      const index = axis.findIndex((day) => day.startsWith(`${month}-`))
      if (index >= 0) tickIndexes.add(index)
    })
    tickIndexes.add(axis.length - 1)
    const xTicks = [...tickIndexes]
      .sort((left, right) => left - right)
      .map((index, position, all) => {
        const anchor: 'start' | 'middle' | 'end' =
          position === 0 ? 'start' : position === all.length - 1 ? 'end' : 'middle'
        return { x: xOf(index), label: axis[index], anchor }
      })
    return { axis, scale, innerHeight, xOf, yOf, lines, xTicks }
  }, [dates, values])

  if (!model) {
    return (
      <div className="flex h-[220px] items-center justify-center text-[13px] text-muted-foreground">
        {emptyText}
      </div>
    )
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || model.axis.length === 0) return
    const svgX = ((event.clientX - rect.left) / rect.width) * CHART_WIDTH
    const innerWidth = CHART_WIDTH - CHART_PADDING.left - CHART_PADDING.right
    if (svgX < CHART_PADDING.left - 8 || svgX > CHART_PADDING.left + innerWidth + 8) {
      setHoverIndex(null)
      return
    }
    const ratio = Math.min(1, Math.max(0, (svgX - CHART_PADDING.left) / innerWidth))
    setHoverIndex(Math.round(ratio * Math.max(0, model.axis.length - 1)))
  }

  const hoverDay = hoverIndex == null ? null : model.axis[hoverIndex]
  const hoverX = hoverIndex == null ? null : model.xOf(hoverIndex)
  const hoveredValues: Array<{
    year: string
    color: string
    point: YearOverlayPoint
  }> =
    hoverIndex == null
      ? []
      : model.lines
          .map((line) => {
            const nearest = line.points.reduce<YearOverlayPoint | null>(
              (best, point) => {
                if (!best) return point
                return Math.abs(point.axisIndex - hoverIndex) <
                  Math.abs(best.axisIndex - hoverIndex)
                  ? point
                  : best
              },
              null,
            )
            return nearest && Math.abs(nearest.axisIndex - hoverIndex) <= 4
              ? { year: line.year, color: line.color, point: nearest }
              : null
          })
          .filter(
            (
              item,
            ): item is { year: string; color: string; point: YearOverlayPoint } =>
              item !== null,
          )
          .sort((left, right) => right.year.localeCompare(left.year))

  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-[11px]">
        {model.lines.map((line) => (
          <span key={line.year} className="flex items-center gap-1.5">
            <span
              className="inline-block h-[2px] w-4 rounded-full"
              style={{ backgroundColor: line.color }}
            />
            <span className="font-mono text-muted-foreground">{line.year}</span>
          </span>
        ))}
      </div>
      <div
        className="relative"
        onPointerMove={handlePointerMove}
        onPointerLeave={() => setHoverIndex(null)}
      >
        <svg
          viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
          className="h-auto w-full select-none"
          role="img"
          aria-label={ariaLabel}
        >
          {model.scale.ticks.map((tick) => (
            <g key={tick}>
              <line
                x1={CHART_PADDING.left}
                x2={CHART_WIDTH - CHART_PADDING.right}
                y1={model.yOf(tick)}
                y2={model.yOf(tick)}
                stroke="hsl(var(--border))"
                strokeWidth={0.5}
                strokeDasharray="2 4"
              />
              <text
                x={CHART_PADDING.left - 8}
                y={model.yOf(tick) + 3.5}
                textAnchor="end"
                fill="hsl(var(--muted-foreground))"
                fontSize={10}
              >
                {tickLabel(tick, model.scale.step)}
              </text>
            </g>
          ))}
          <text
            x={4}
            y={10}
            textAnchor="start"
            fill="hsl(var(--muted-foreground))"
            fontSize={9}
          >
            {unitLabel}
          </text>
          {model.lines.map((line, index) => (
            <g key={line.year}>
              <path
                d={line.path}
                fill="none"
                stroke={line.color}
                strokeWidth={index === model.lines.length - 1 ? 2.2 : 1.6}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              {line.tail && (
                <circle
                  cx={line.tail.x}
                  cy={line.tail.y}
                  r={index === model.lines.length - 1 ? 2.8 : 2.2}
                  fill={line.color}
                />
              )}
            </g>
          ))}
          {model.xTicks.map((tick) => (
            <text
              key={tick.label}
              x={tick.x}
              y={CHART_HEIGHT - 10}
              textAnchor={tick.anchor}
              fill="hsl(var(--muted-foreground))"
              fontSize={10}
            >
              {tick.label}
            </text>
          ))}
          {hoverIndex != null && hoverX != null && (
            <>
              <line
                x1={hoverX}
                x2={hoverX}
                y1={CHART_PADDING.top}
                y2={CHART_PADDING.top + model.innerHeight}
                stroke="hsl(var(--muted-foreground))"
                strokeWidth={0.8}
                strokeDasharray="3 3"
                opacity={0.6}
              />
              {hoveredValues.map((item) => (
                <circle
                  key={item.year}
                  cx={item.point.x}
                  cy={item.point.y}
                  r={2.8}
                  fill={item.color}
                  stroke="hsl(var(--background))"
                  strokeWidth={1}
                />
              ))}
            </>
          )}
        </svg>
        {hoverDay != null && hoveredValues.length > 0 && (
          <div
            className="pointer-events-none absolute top-1 z-10 min-w-[150px] max-w-[240px] rounded-md border border-border bg-popover px-2.5 py-2 shadow-sm"
            style={{
              left: `${((hoverX ?? 0) / CHART_WIDTH) * 100}%`,
              transform:
                hoverX != null && hoverX > CHART_WIDTH * 0.62
                  ? 'translateX(-100%)'
                  : 'translateX(0)',
            }}
          >
            <div className="font-mono text-[11px] text-muted-foreground">{hoverDay}</div>
            <div className="mt-1 space-y-0.5">
              {hoveredValues.map((item) => (
                <div key={item.year} className="flex items-center gap-1.5 text-[11px]">
                  <span
                    className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
                    style={{ backgroundColor: item.color }}
                  />
                  <span className="font-mono text-muted-foreground">{item.year}</span>
                  {item.point.day !== hoverDay && (
                    <span className="font-mono text-[10px] text-muted-foreground/60">
                      {item.point.day}
                    </span>
                  )}
                  <span className="ml-auto whitespace-nowrap font-mono font-medium text-foreground">
                    {formatValue(item.point.value, 0)}
                    {valueUnit ? ` ${valueUnit}` : ''}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function CoalDashboard({
  data,
  onChanged,
}: {
  data: MetricsPayload
  onChanged: () => void
}) {
  const { t } = useTranslation(['configuration', 'common'])
  const { toast } = useToast()
  const dateFieldId = useId()
  const priceFieldId = useId()
  const unit = t('etfShares.coal.unit')
  const points = useMemo<CoalPoint[]>(() => {
    const values = data.series.cci_5500 ?? []
    const rows: CoalPoint[] = []
    data.dates.forEach((day, index) => {
      const value = values[index]
      if (typeof value === 'number' && Number.isFinite(value)) {
        rows.push({ date: day, price: value })
      }
    })
    return rows.reverse()
  }, [data])
  const [editor, setEditor] = useState<CoalEditor | null>(null)
  const [formDate, setFormDate] = useState('')
  const [formPrice, setFormPrice] = useState('')
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const isEditing = editor?.mode === 'edit'

  const openCreate = () => {
    setEditor({ mode: 'create' })
    setFormDate(localDateString())
    setFormPrice('')
    setFormError('')
  }

  const openEdit = (point: CoalPoint) => {
    setEditor({ mode: 'edit', original: point.date })
    setFormDate(point.date)
    setFormPrice(String(Math.round(point.price)))
    setFormError('')
  }

  const closeEditor = () => {
    if (saving) return
    setEditor(null)
    setFormError('')
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!editor) return
    if (!/^\d{4}-\d{2}-\d{2}$/.test(formDate)) {
      setFormError(t('etfShares.coal.data.invalidDate'))
      return
    }
    const price = Number(formPrice.trim())
    if (!/^\d+$/.test(formPrice.trim()) || !Number.isFinite(price) || price <= 0) {
      setFormError(t('etfShares.coal.data.invalidPrice'))
      return
    }
    const duplicate = points.some(
      (point) =>
        point.date === formDate &&
        !(editor.mode === 'edit' && editor.original === formDate),
    )
    if (duplicate) {
      setFormError(t('etfShares.coal.data.dateExists'))
      return
    }
    setSaving(true)
    setFormError('')
    try {
      if (editor.mode === 'edit') {
        await etfSharesApi.updateCoalPoint(editor.original, { date: formDate, price })
        toast(t('etfShares.coal.data.updated'), 'success')
      } else {
        await etfSharesApi.createCoalPoint({ date: formDate, price })
        toast(t('etfShares.coal.data.created'), 'success')
      }
      setSaving(false)
      setEditor(null)
      onChanged()
    } catch (error) {
      setSaving(false)
      setFormError(
        error instanceof Error
          ? error.message
          : t('etfShares.coal.data.saveFailed'),
      )
    }
  }

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-1 gap-2.5 sm:max-w-xs">
        {data.metrics.map((metric) => {
          const key = COAL_METRIC_KEYS[metric.id as keyof typeof COAL_METRIC_KEYS]
          return (
            <MetricCard
              key={metric.id}
              metric={metric}
              label={key ? t(key) : metric.id}
              unit={unit}
              color={COAL_METRIC_COLORS[metric.id] ?? COAL_METRIC_COLORS.cci_5500}
              digits={0}
            />
          )
        })}
      </section>

      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border/60 px-4 py-2.5">
          <span
            className="h-4 w-1 rounded"
            style={{ backgroundColor: COAL_METRIC_COLORS.cci_5500 }}
          />
          <h2 className="text-[13px] font-semibold">
            {t('etfShares.coal.charts.cciTitle')}
          </h2>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {t('etfShares.coal.charts.daily')}
          </span>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {t('etfShares.coal.charts.overlay')}
          </span>
          {data.as_of && (
            <span className="ml-auto font-mono text-[11px] text-muted-foreground">
              {data.as_of}
            </span>
          )}
        </div>
        <div className="px-2 pb-2 pt-3 md:px-4">
          <p className="mb-2 px-1 text-[11px] text-muted-foreground/80">
            {t('etfShares.coal.charts.overlayHint')}
          </p>
          <YearOverlayChart
            dates={data.dates}
            values={data.series.cci_5500 ?? []}
            emptyText={t('etfShares.coal.charts.empty')}
            unitLabel={t('etfShares.coal.charts.yUnit')}
            valueUnit={unit}
            ariaLabel={t('etfShares.coal.charts.overlayAria')}
          />
        </div>
      </section>

      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 px-4 py-2.5">
          <span
            className="h-4 w-1 rounded"
            style={{ backgroundColor: COAL_METRIC_COLORS.cci_5500 }}
          />
          <h2 className="text-[13px] font-semibold">
            {t('etfShares.coal.data.title')}
          </h2>
          <Badge variant="secondary" className="text-[10px]">
            {t('etfShares.coal.data.count', { count: points.length })}
          </Badge>
          <Button
            variant="outline"
            size="sm"
            className="ml-auto h-7 px-2 text-[11px]"
            onClick={openCreate}
          >
            <Plus className="h-3.5 w-3.5" />
            {t('etfShares.coal.data.add')}
          </Button>
        </div>
        {points.length === 0 ? (
          <p className="px-4 py-6 text-center text-[12px] text-muted-foreground">
            {t('etfShares.coal.data.empty')}
          </p>
        ) : (
          <div className="max-h-[340px] overflow-auto scrollbar">
            <table className="w-full text-[12px]">
              <thead className="sticky top-0 z-10 bg-card text-left text-[11px] text-muted-foreground">
                <tr className="border-b border-border/60">
                  <th className="px-4 py-2 font-medium">
                    {t('etfShares.coal.data.date')}
                  </th>
                  <th className="px-4 py-2 text-right font-medium">
                    {t('etfShares.coal.data.price', { unit })}
                  </th>
                  <th className="w-20 px-3 py-2 text-right font-medium">
                    {t('etfShares.coal.data.actions')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {points.map((point) => (
                  <tr
                    key={point.date}
                    className="border-b border-border/40 last:border-0 hover:bg-accent/20"
                  >
                    <td className="px-4 py-1.5 font-mono">{point.date}</td>
                    <td className="px-4 py-1.5 text-right font-mono">
                      {formatValue(point.price, 0)}
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-[11px]"
                        onClick={() => openEdit(point)}
                      >
                        <Pencil className="h-3 w-3" />
                        {t('common:actions.edit')}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="space-y-1.5 border-t border-border/60 px-4 py-2.5 text-[11px] text-muted-foreground/80">
          <p>{t('etfShares.coal.data.hint')}</p>
          {data.storage && (
            <div className="space-y-1 rounded-lg bg-muted/40 px-2.5 py-2">
              <p className="flex items-start gap-1.5">
                <Database className="mt-0.5 h-3 w-3 shrink-0" />
                <span className="break-all">
                  {t('etfShares.coal.data.storage', {
                    database: data.storage.database,
                    table: data.storage.table,
                  })}
                </span>
              </p>
              <p className="break-all pl-[18px] text-muted-foreground/70">
                {t('etfShares.coal.data.seedFile', {
                  path: data.storage.seed,
                })}
              </p>
            </div>
          )}
        </div>
      </section>

      <p className="px-1 text-[11px] text-muted-foreground/80">
        {t('etfShares.coal.caveat')}
      </p>

      <Dialog
        open={editor !== null}
        onOpenChange={(open) => {
          if (!open) closeEditor()
        }}
      >
        <DialogContent className="sm:max-w-[420px]">
          <DialogHeader>
            <DialogTitle>
              {isEditing
                ? t('etfShares.coal.data.editTitle')
                : t('etfShares.coal.data.addTitle')}
            </DialogTitle>
            <DialogDescription>
              {isEditing
                ? t('etfShares.coal.data.editHint')
                : t('etfShares.coal.data.addHint')}
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-3"
            onSubmit={(event) => {
              void submit(event)
            }}
          >
            <div className="space-y-1.5">
              <Label htmlFor={dateFieldId}>
                {t('etfShares.coal.data.dateLabel')}
              </Label>
              <Input
                id={dateFieldId}
                type="date"
                value={formDate}
                onChange={(event) => {
                  setFormDate(event.target.value)
                  setFormError('')
                }}
                required
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={priceFieldId}>
                {t('etfShares.coal.data.priceLabel', { unit })}
              </Label>
              <Input
                id={priceFieldId}
                type="number"
                min={1}
                step={1}
                inputMode="numeric"
                value={formPrice}
                onChange={(event) => {
                  setFormPrice(event.target.value)
                  setFormError('')
                }}
                required
              />
            </div>
            {formError && (
              <p role="alert" className="text-[12px] text-red-500">
                {formError}
              </p>
            )}
            <div className="flex justify-end gap-2 pt-1">
              <Button
                type="button"
                variant="outline"
                onClick={closeEditor}
                disabled={saving}
              >
                {t('common:actions.cancel')}
              </Button>
              <Button type="submit" disabled={saving}>
                {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {saving
                  ? t('etfShares.coal.data.saving')
                  : t('common:actions.save')}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function FundCard({
  fund,
  color,
  active,
  unit,
  onClick,
}: {
  fund: FundSeries
  color: string
  active: boolean
  unit: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-xl border p-3 text-left transition-colors ${
        active ? 'bg-accent/30' : 'border-border/60 bg-card/60 hover:bg-accent/20'
      }`}
      style={active ? { borderColor: color } : undefined}
      title={fund.latest_date ?? undefined}
    >
      <div className="flex items-center gap-1.5">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: color }} />
        <span className="truncate text-[11px] text-muted-foreground">{fund.name}</span>
      </div>
      <div className="mt-0.5 font-mono text-[10px] text-muted-foreground/70">{fund.code}</div>
      <div className="mt-1.5 flex items-baseline gap-1">
        <span className="font-mono text-[17px] font-bold leading-none text-foreground">
          {formatValue(fund.latest)}
        </span>
        <span className="text-[10px] text-muted-foreground">{unit}</span>
      </div>
      <div className={`mt-1 font-mono text-[11px] ${marketSignTextClass(fund.delta)}`}>
        {formatDelta(fund.delta)}
        {unit}
        <span className="ml-1">({formatPercent(fund.delta_pct)})</span>
      </div>
    </button>
  )
}

function PlannedGroup({ group, label }: { group: GroupMeta; label: string }) {
  const { t } = useTranslation('configuration')
  return (
    <section className="card overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border/60 px-4 py-2.5">
        <span className="h-4 w-1 rounded" style={{ backgroundColor: group.accent }} />
        <h2 className="text-[13px] font-semibold">{label}</h2>
        <Badge variant="secondary" className="text-[10px]">
          {t('etfShares.planned')}
        </Badge>
      </div>
      <div className="space-y-3 p-3 md:p-4">
        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
          {[0, 1, 2, 3, 4].map((item) => (
            <div
              key={item}
              className="h-[88px] rounded-xl border border-dashed border-border/70 p-3"
            >
              <div className="h-2 w-16 rounded bg-muted/70" />
              <div className="mt-1.5 h-2 w-10 rounded bg-muted/50" />
              <div className="mt-2.5 h-4 w-20 rounded bg-muted/70" />
              <div className="mt-1.5 h-2 w-24 rounded bg-muted/50" />
            </div>
          ))}
        </div>
        <div className="flex h-[220px] flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border/70 px-6 text-center">
          <p className="max-w-[440px] text-[12px] leading-relaxed text-muted-foreground">
            {t('etfShares.plannedHint')}
          </p>
        </div>
      </div>
    </section>
  )
}

function GroupNavItem({
  group,
  active,
  label,
  plannedLabel,
  onClick,
}: {
  group: GroupMeta
  active: boolean
  label: string
  plannedLabel: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`mt-0.5 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[12px] transition-colors ${
        active
          ? 'bg-accent/60 font-medium text-foreground'
          : 'text-muted-foreground hover:bg-accent/30 hover:text-foreground'
      }`}
    >
      <span
        className="h-1.5 w-1.5 shrink-0 rounded-full"
        style={{
          backgroundColor: group.available ? group.accent : 'hsl(var(--muted-foreground))',
          opacity: group.available ? 1 : 0.75,
        }}
      />
      <span className="truncate">{label}</span>
      {!group.available && (
        <span className="ml-auto shrink-0 rounded bg-muted px-1 py-0.5 text-[9px] text-muted-foreground">
          {plannedLabel}
        </span>
      )}
    </button>
  )
}

function EtfSharesPage() {
  const { t } = useTranslation('configuration')
  const { toast } = useToast()
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [groupId, setGroupId] = useState('hs300')
  const [days, setDays] = useState(90)
  const [curve, setCurve] = useState<CurveResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [filling, setFilling] = useState(false)
  const [hiddenCodes, setHiddenCodes] = useState<string[]>([])
  const [focusedCode, setFocusedCode] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const refreshIntent = useRef(false)

  const groupLabel = useCallback(
    (group: GroupMeta | null | undefined) => {
      if (!group) return ''
      const key = GROUP_LABEL_KEYS[group.id as keyof typeof GROUP_LABEL_KEYS]
      return key ? t(key) : group.label
    },
    [t],
  )

  const categoryLabel = (category: GroupMeta['category']) =>
    category === 'broad'
      ? t('etfShares.categories.broad')
      : t('etfShares.categories.commodity')

  useEffect(() => {
    let cancelled = false
    etfSharesApi
      .catalog()
      .then((data) => {
        if (cancelled) return
        setCatalog(data)
        setGroupId(data.default_group)
        setDays(data.default_days)
      })
      .catch(() => {
        if (cancelled) return
        toast(t('etfShares.errors.catalogFailed'), 'error')
      })
    return () => {
      cancelled = true
    }
  }, [toast, t])

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const initialAction = refreshIntent.current ? 'refresh' : 'curve'
    refreshIntent.current = false

    const planned = catalog?.groups.some((group) => group.id === groupId && !group.available)
    if (planned) {
      setLoading(false)
      setFilling(false)
      setCurve(null)
      setFocusedCode(null)
      return
    }

    setLoading(true)
    setFilling(false)
    setCurve(null)

    const run = async (action: 'curve' | 'refresh') => {
      const full = groupId === 'coalPrice' || groupId === 'aluminumInventory'
      try {
        const data =
          action === 'refresh'
            ? await etfSharesApi.refresh(groupId, days, full)
            : await etfSharesApi.curve(groupId, days, full)
        if (cancelled) return
        setCurve(data)
        setLoading(false)
        if (isMetricsPayload(data)) {
          setFocusedCode(null)
          setFilling(false)
        } else {
          setFocusedCode((current) => focusAfterLoad(data.group, data.funds, current))
          const pending = !data.coverage.complete
          setFilling(pending)
          if (pending) {
            timer = window.setTimeout(() => {
              if (!cancelled) void run('curve')
            }, POLL_INTERVAL_MS)
          }
        }
      } catch (error) {
        if (cancelled) return
        setLoading(false)
        setFilling(false)
        toast(
          error instanceof Error ? error.message : t('etfShares.errors.loadFailed'),
          'error',
        )
      }
    }

    void run(initialAction)
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [groupId, days, reloadToken, toast, t])

  const groups = catalog?.groups ?? []
  const activeGroup = groups.find((group) => group.id === groupId) ?? null
  const isMarginGroup = activeGroup?.id === 'margin'
  const isCoalGroup = activeGroup?.id === 'coalPrice'
  const isAluminumGroup = activeGroup?.id === 'aluminum'
  const isInventoryGroup = activeGroup?.id === 'aluminumInventory'
  const isThreeGorgesGroup = activeGroup?.id === 'threeGorges'
  const pageTitle = isCoalGroup
    ? t('etfShares.coal.title')
    : isInventoryGroup
      ? t('etfShares.aluminumInventory.title')
      : isAluminumGroup
        ? t('etfShares.aluminum.title')
        : isThreeGorgesGroup
          ? t('etfShares.threeGorges.title')
          : isMarginGroup
            ? t('etfShares.margin.title')
            : t('etfShares.title')
  const pageBadge = isCoalGroup
    ? t('etfShares.coal.badge')
    : isInventoryGroup
      ? t('etfShares.aluminumInventory.badge')
      : isAluminumGroup
        ? t('etfShares.aluminum.badge')
        : isThreeGorgesGroup
          ? t('etfShares.threeGorges.badge')
          : isMarginGroup
            ? t('etfShares.margin.badge')
            : t('etfShares.badge')
  const pageSubtitle = isCoalGroup
    ? t('etfShares.coal.subtitle')
    : isInventoryGroup
      ? t('etfShares.aluminumInventory.subtitle')
      : isAluminumGroup
        ? t('etfShares.aluminum.subtitle')
        : isThreeGorgesGroup
          ? t('etfShares.threeGorges.subtitle')
          : isMarginGroup
            ? t('etfShares.margin.subtitle')
            : t('etfShares.subtitle')
  const cardUnit = t('etfShares.unit')
  const chartTitle = t('etfShares.chart.title')
  const chartEmpty = t('etfShares.chart.empty')
  const chartYUnit = t('etfShares.chart.yUnit')
  const loadingText = isCoalGroup
    ? t('etfShares.coal.loading')
    : isInventoryGroup
      ? t('etfShares.aluminumInventory.loading')
      : isAluminumGroup
        ? t('etfShares.aluminum.loading')
        : isThreeGorgesGroup
          ? t('etfShares.threeGorges.loading')
          : isMarginGroup
            ? t('etfShares.margin.loading')
            : t('etfShares.loading')
  const noDataText = isCoalGroup
    ? t('etfShares.coal.noData')
    : isInventoryGroup
      ? t('etfShares.aluminumInventory.noData')
      : isAluminumGroup
        ? t('etfShares.aluminum.noData')
        : isThreeGorgesGroup
          ? t('etfShares.threeGorges.noData')
          : isMarginGroup
            ? t('etfShares.margin.noData')
            : t('etfShares.errors.noData')
  const metricsCurve = isMetricsPayload(curve) ? curve : null
  const shareCurve = curve && !isMetricsPayload(curve) ? curve : null
  const hasData = metricsCurve
    ? metricsCurve.metrics.length > 0 && metricsCurve.dates.length > 0
    : Boolean(shareCurve && shareCurve.funds.length > 0)
  const fundMetas = activeGroup?.funds ?? []
  const curveFunds = shareCurve?.funds ?? []
  const rangeOptions = (catalog?.allowed_days ?? [30, 60, 90, 120]).filter(
    (value) => value <= 250,
  )
  const rangeLabel = (value: number) =>
    isThreeGorgesGroup
      ? t('etfShares.threeGorges.rangeDays', { count: value })
      : t('etfShares.rangeDays', { count: value })
  const categories = (['broad', 'commodity'] as const)
    .map((category) => ({
      id: category,
      items: groups.filter((group) => group.category === category),
    }))
    .filter((category) => category.items.length > 0)

  const colorFor = useCallback(
    (code: string) => {
      const index = fundMetas.findIndex((fund) => fund.code === code)
      if (index >= 0) return seriesColor(index)
      const fallback = curveFunds.findIndex((fund) => fund.code === code)
      return seriesColor(fallback)
    },
    [fundMetas, curveFunds],
  )

  const chartSeries = curveFunds
    .filter((fund) => !hiddenCodes.includes(fund.code))
    .filter((fund) => !focusedCode || fund.code === focusedCode)
    .map((fund) => ({
      code: fund.code,
      name: fund.name,
      color: colorFor(fund.code),
      values: fund.values,
    }))

  const focusedFund =
    focusedCode && shareCurve
      ? shareCurve.funds.find((fund) => fund.code === focusedCode) ?? null
      : null
  const detailDailyTitle = t('etfShares.detail.dailyTitle')
  const detailDailyHint = t('etfShares.detail.dailyHint')
  const detailDailyUnit = t('etfShares.detail.dailyYUnit')
  const detailDailyEmpty = t('etfShares.detail.dailyEmpty')
  const detailRecentTitle = t('etfShares.detail.recentTitle')
  const detailRecentUnit = t('etfShares.detail.recentYUnit')
  const detailRecentEmpty = t('etfShares.detail.recentEmpty')

  const selectGroup = (id: string) => {
    setGroupId(id)
    const nextOptions = (catalog?.allowed_days ?? [30, 60, 90, 120]).filter(
      (value) => value <= 250,
    )
    if (!nextOptions.includes(days)) {
      setDays(catalog?.default_days ?? 90)
    }
    setHiddenCodes([])
    setFocusedCode(null)
  }

  const toggleHidden = (code: string) => {
    setHiddenCodes((current) =>
      current.includes(code) ? current.filter((item) => item !== code) : [...current, code],
    )
  }

  const refresh = () => {
    refreshIntent.current = true
    setReloadToken((current) => current + 1)
  }

  const handleCoalChanged = () => {
    refreshIntent.current = false
    setReloadToken((current) => current + 1)
  }

  const updatedLine = curve?.as_of
    ? t('etfShares.updatedAt', { date: curve.as_of })
    : t('etfShares.updatedUnknown')
  const updatedCount =
    curve && curve.dates.length > 0
      ? isCoalGroup
        ? t('etfShares.coal.updatedCount', { count: curve.coverage.trading_days })
        : isInventoryGroup
          ? t('etfShares.aluminumInventory.updatedCount', {
              count: curve.coverage.trading_days,
            })
          : isThreeGorgesGroup
            ? t('etfShares.threeGorges.updatedCount', {
                count: curve.coverage.trading_days,
              })
          : t('etfShares.updatedCount', { count: curve.coverage.trading_days })
      : ''

  return (
    <div className="w-full space-y-4">
      <section className="card overflow-hidden">
        <div className="flex flex-col gap-3 p-4 md:flex-row md:items-start md:justify-between md:p-5">
          <div className="flex gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500 to-blue-500 shadow-sm">
              <LineChart className="h-5 w-5 text-white" />
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-lg font-bold md:text-xl">{pageTitle}</h1>
                <Badge variant="secondary">{pageBadge}</Badge>
              </div>
              {activeGroup?.available !== false && (
                <>
                  <p className="mt-1 text-[12px] text-muted-foreground md:text-[13px]">
                    {pageSubtitle}
                  </p>
                  <p className="mt-0.5 text-[11px] text-muted-foreground/80">
                    {updatedLine}
                    {updatedCount}
                  </p>
                </>
              )}
            </div>
          </div>
          {activeGroup?.available !== false && (
            <div className="flex items-center gap-2">
              {!isCoalGroup && !isInventoryGroup && (
                <Select
                  value={String(days)}
                  onValueChange={(value) => setDays(Number(value))}
                >
                  <SelectTrigger
                    className="h-8 w-[190px] text-[12px]"
                    aria-label={rangeLabel(days)}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {rangeOptions.map((value) => (
                      <SelectItem key={value} value={String(value)}>
                        {rangeLabel(value)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
              <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
                <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
                {t('etfShares.refresh')}
              </Button>
            </div>
          )}
        </div>
        {filling && curve && (
          <div className="flex items-center gap-2 border-t border-border/60 bg-accent/20 px-4 py-1.5 text-[11px] text-muted-foreground md:px-5">
            <Loader2 className="h-3 w-3 animate-spin" />
            {t('etfShares.filling', {
              have: curve.coverage.trading_days,
              need: curve.coverage.target,
            })}
          </div>
        )}
      </section>

      <div className="grid gap-4 lg:grid-cols-[188px_minmax(0,1fr)]">
        <aside className="card hidden h-fit p-2 lg:block">
          {categories.map((category) => (
            <div key={category.id}>
              <div className="px-2 py-1.5 text-[10px] font-medium tracking-wide text-muted-foreground/70">
                {categoryLabel(category.id)}
              </div>
              {category.items.map((group) => (
                <GroupNavItem
                  key={group.id}
                  group={group}
                  active={group.id === groupId}
                  label={groupLabel(group)}
                  plannedLabel={t('etfShares.planned')}
                  onClick={() => selectGroup(group.id)}
                />
              ))}
            </div>
          ))}
        </aside>

        <div className="min-w-0 space-y-4">
          <div className="space-y-2 lg:hidden">
            {categories.map((category) => (
              <div key={category.id}>
                <div className="mb-1 px-1 text-[10px] font-medium tracking-wide text-muted-foreground/70">
                  {categoryLabel(category.id)}
                </div>
                <div className="-mx-1 flex gap-1.5 overflow-x-auto scrollbar-none px-1">
                  {category.items.map((group) => (
                    <button
                      key={group.id}
                      type="button"
                      onClick={() => selectGroup(group.id)}
                      className={`shrink-0 rounded-full border px-3 py-1 text-[11px] transition-colors ${
                        group.id === groupId
                          ? 'border-transparent bg-accent/70 font-medium text-foreground'
                          : 'border-border/60 text-muted-foreground'
                      }`}
                    >
                      {groupLabel(group)}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>

          {activeGroup && !activeGroup.available ? (
            <PlannedGroup group={activeGroup} label={groupLabel(activeGroup)} />
          ) : loading ? (
            <section className="card flex h-[320px] items-center justify-center gap-2 text-[13px] text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              {loadingText}
            </section>
          ) : isCoalGroup && metricsCurve ? (
            <CoalDashboard data={metricsCurve} onChanged={handleCoalChanged} />
          ) : isAluminumGroup && metricsCurve ? (
            <AluminumDashboard data={metricsCurve} />
          ) : isInventoryGroup && metricsCurve ? (
            <AluminumInventoryDashboard data={metricsCurve} />
          ) : isThreeGorgesGroup && metricsCurve ? (
            <ThreeGorgesDashboard data={metricsCurve} />
          ) : !hasData ? (
            <section className="card flex h-[320px] flex-col items-center justify-center gap-2 px-6 text-center">
              <p className="text-[13px] text-muted-foreground">{noDataText}</p>
              <Button variant="outline" size="sm" onClick={refresh}>
                <RefreshCw className="h-3.5 w-3.5" />
                {t('etfShares.refresh')}
              </Button>
            </section>
          ) : isMarginGroup && metricsCurve ? (
            <MarginDashboard data={metricsCurve} />
          ) : shareCurve ? (
            <>
              <section className="card overflow-hidden">
                <div className="flex items-center gap-2 border-b border-border/60 px-4 py-2.5">
                  <span
                    className="h-4 w-1 rounded"
                    style={{ backgroundColor: activeGroup?.accent }}
                  />
                  <h2 className="text-[13px] font-semibold">
                    {activeGroup ? groupLabel(activeGroup) : ''}
                  </h2>
                  <Badge variant="secondary" className="text-[10px]">
                    {t('etfShares.fundCount', { count: curveFunds.length })}
                  </Badge>
                  <Badge variant="secondary" className="text-[10px]">
                    {t('etfShares.tradingDays', { count: shareCurve.coverage.trading_days })}
                  </Badge>
                  {shareCurve.as_of && (
                    <Badge variant="secondary" className="text-[10px]">
                      {shareCurve.as_of}
                    </Badge>
                  )}
                </div>
                <div className="grid grid-cols-2 gap-2.5 p-3 sm:grid-cols-3 md:p-4 lg:grid-cols-5">
                  {curveFunds.map((fund) => (
                    <FundCard
                      key={fund.code}
                      fund={fund}
                      color={colorFor(fund.code)}
                      active={focusedCode === fund.code}
                      unit={cardUnit}
                      onClick={() =>
                        setFocusedCode((current) => (current === fund.code ? null : fund.code))
                      }
                    />
                  ))}
                </div>
              </section>

              <section className="card overflow-hidden">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-4 py-3">
                  <div className="text-[13px] font-semibold">{chartTitle}</div>
                  <div className="text-[11px] text-muted-foreground">
                    {t('etfShares.chart.hint')}
                  </div>
                </div>
                <div className="px-2 pb-2 pt-3 md:px-4">
                  <div className="mb-1 flex flex-wrap items-center gap-1.5">
                    {curveFunds.map((fund) => {
                      const hidden = hiddenCodes.includes(fund.code)
                      const color = colorFor(fund.code)
                      return (
                        <button
                          key={fund.code}
                          type="button"
                          onClick={() => toggleHidden(fund.code)}
                          aria-pressed={!hidden}
                          className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] transition-colors ${
                            hidden
                              ? 'border-border/50 text-muted-foreground/50'
                              : 'border-border/70 text-foreground/80 hover:bg-accent/40'
                          }`}
                        >
                          <span
                            className="h-2 w-2 shrink-0 rounded-sm border"
                            style={{
                              borderColor: color,
                              backgroundColor: hidden ? 'transparent' : color,
                            }}
                          />
                          <span className={hidden ? 'line-through' : ''}>{fund.name}</span>
                          <span className="font-mono text-[10px] text-muted-foreground">
                            {fund.code}
                          </span>
                        </button>
                      )
                    })}
                  </div>
                  <ShareChart
                    dates={shareCurve.dates}
                    series={chartSeries}
                    emptyText={chartEmpty}
                    unitLabel={chartYUnit}
                    ariaLabel={chartTitle}
                  />
                </div>
              </section>

              {focusedFund && (
                <div className="grid gap-4 xl:grid-cols-2">
                  <section className="card overflow-hidden">
                    <div className="flex items-center gap-2 border-b border-border/60 px-4 py-2.5">
                      <span
                        className="h-4 w-1 rounded"
                        style={{ backgroundColor: colorFor(focusedFund.code) }}
                      />
                      <h2 className="text-[13px] font-semibold">{detailDailyTitle}</h2>
                      <span
                        className="truncate text-[12px]"
                        style={{ color: colorFor(focusedFund.code) }}
                      >
                        {focusedFund.name}
                      </span>
                      <Badge variant="secondary" className="ml-auto shrink-0 text-[10px]">
                        {focusedFund.code}
                      </Badge>
                    </div>
                    <div className="px-2 pb-2 pt-3 md:px-4">
                      <p className="mb-1 text-[11px] text-muted-foreground">{detailDailyHint}</p>
                      <DailyChangeChart
                        dates={shareCurve.dates}
                        values={focusedFund.values}
                        emptyText={detailDailyEmpty}
                        unitLabel={detailDailyUnit}
                        valueUnit={cardUnit}
                        ariaLabel={`${detailDailyTitle} ${focusedFund.name}`}
                      />
                    </div>
                  </section>

                  <section className="card overflow-hidden">
                    <div className="flex items-center gap-2 border-b border-border/60 px-4 py-2.5">
                      <span
                        className="h-4 w-1 rounded"
                        style={{ backgroundColor: colorFor(focusedFund.code) }}
                      />
                      <h2 className="text-[13px] font-semibold">{detailRecentTitle}</h2>
                      <span
                        className="truncate text-[12px]"
                        style={{ color: colorFor(focusedFund.code) }}
                      >
                        {focusedFund.name}
                      </span>
                      <Badge variant="secondary" className="ml-auto shrink-0 text-[10px]">
                        {focusedFund.code}
                      </Badge>
                    </div>
                    <div className="px-2 pb-2 pt-3 md:px-4">
                      <RecentTrendChart
                        dates={shareCurve.dates.slice(-10)}
                        values={focusedFund.values.slice(-10)}
                        color={colorFor(focusedFund.code)}
                        emptyText={detailRecentEmpty}
                        unitLabel={detailRecentUnit}
                        ariaLabel={`${detailRecentTitle} ${focusedFund.name}`}
                      />
                    </div>
                  </section>
                </div>
              )}

              <p className="px-1 text-[11px] text-muted-foreground/80">
                {t('etfShares.caveat')}
              </p>
            </>
          ) : null}
        </div>
      </div>
    </div>
  )
}

export default EtfSharesPage
