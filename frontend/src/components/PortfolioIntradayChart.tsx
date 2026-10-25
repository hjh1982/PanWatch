import { useId, useMemo, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { useTranslation } from 'react-i18next'
import type { PortfolioIntradayPoint } from '@panwatch/api'
import { getCurrentLocale } from '@/i18n'
import { useMarketColors } from '@/hooks/use-market-colors'
import { marketColorWithAlpha, marketDirection, marketSignTextClass } from '@/lib/market-colors'

const WIDTH = 720
const HEIGHT = 130
const PAD = { top: 16, right: 14, bottom: 26, left: 62 }

interface Props {
  points: PortfolioIntradayPoint[]
  /** 昨收市值,作为日内涨跌基准线。 */
  baseline: number
  emptyText: string
}

function formatMoney(value: number, locale: string): string {
  return value.toLocaleString(locale, { maximumFractionDigits: 0 })
}

function formatWan(value: number, locale: string): string {
  return locale === 'en-US'
    ? `${(value / 1_000_000).toFixed(2)}M`
    : `${(value / 10_000).toFixed(0)}万`
}

/**
 * 持仓日内市值曲线。纯 SVG + 指针追踪,不引图表依赖;
 * 虚线为昨收市值基准,曲线颜色跟随当日涨跌,悬停显示该分钟市值与日内变动。
 */
export function PortfolioIntradayChart({ points, baseline, emptyText }: Props) {
  const { palette } = useMarketColors()
  const { t } = useTranslation('configuration')
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const gradientId = useId()
  const locale = getCurrentLocale() === 'en-US' ? 'en-US' : 'zh-CN'

  const geometry = useMemo(() => {
    if (points.length < 2) return null
    const values = points.map(p => p.market_value)
    const rawMin = Math.min(...values, baseline)
    const rawMax = Math.max(...values, baseline)
    const padding = (rawMax - rawMin || Math.abs(rawMax) * 0.01 || 1) * 0.12
    const minV = Math.max(0, rawMin - padding)
    const maxV = rawMax + padding
    const range = maxV - minV || 1
    const innerW = WIDTH - PAD.left - PAD.right
    const innerH = HEIGHT - PAD.top - PAD.bottom
    const yOf = (value: number) => PAD.top + innerH - ((value - minV) / range) * innerH
    const coords = points.map((p, i) => ({
      x: PAD.left + (i / (points.length - 1)) * innerW,
      y: yOf(p.market_value),
    }))
    const line = coords
      .map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(2)},${c.y.toFixed(2)}`)
      .join(' ')
    const area = `${line} L${coords[coords.length - 1].x.toFixed(2)},${PAD.top + innerH} L${coords[0].x.toFixed(2)},${PAD.top + innerH} Z`
    const ticks = Array.from({ length: 4 }, (_, i) => {
      const value = minV + (range / 3) * i
      return { value, y: yOf(value) }
    })
    const labelIdx = Array.from(
      new Set(
        ['09:30', '10:30', '13:00', '14:00', '15:00']
          .map(target => points.findIndex(p => p.time === target))
          .filter(i => i >= 0),
      ),
    )
    const ticksIdx = labelIdx.length >= 3 ? labelIdx : Array.from(
      new Set([
        0,
        Math.floor((points.length - 1) * 0.25),
        Math.floor((points.length - 1) * 0.5),
        Math.floor((points.length - 1) * 0.75),
        points.length - 1,
      ]),
    )
    return { coords, line, area, ticks, labelIdx: ticksIdx, yOf }
  }, [points, baseline])

  if (!geometry) {
    return (
      <div className="h-[110px] flex items-center justify-center text-muted-foreground text-sm">
        {emptyText}
      </div>
    )
  }

  const current = points[points.length - 1].market_value
  const change = current - baseline
  const changePct = baseline > 0 ? (change / baseline) * 100 : 0
  const direction = marketDirection(change)
  const tone =
    direction === 'up' ? palette.up : direction === 'down' ? palette.down : null
  const stroke = tone?.bright ?? palette.flat
  const fill = marketColorWithAlpha(stroke, 0.12)

  const active = hoverIndex == null ? null : points[hoverIndex]
  const activeCoord = hoverIndex == null ? null : geometry.coords[hoverIndex]
  const activeChange = active ? active.market_value - baseline : 0

  const handlePointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width) return
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
    setHoverIndex(Math.round(ratio * (points.length - 1)))
  }

  const tooltipLeft = activeCoord ? `${(activeCoord.x / WIDTH) * 100}%` : '0%'
  const flipTooltip = activeCoord ? activeCoord.x > WIDTH * 0.66 : false

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span>
          {t('stocksPage.intraday.current')}
          <span className={`ml-1.5 font-mono text-[15px] font-semibold ${marketSignTextClass(change)}`}>
            {formatWan(current, locale)}
          </span>
        </span>
        <span>
          {t('stocksPage.intraday.change')}
          <span className={`ml-1.5 font-mono ${marketSignTextClass(change)}`}>
            {change >= 0 ? '+' : ''}
            {formatMoney(change, locale)}
            <span className="ml-1">
              ({changePct >= 0 ? '+' : ''}
              {changePct.toFixed(2)}%)
            </span>
          </span>
        </span>
        <span>
          {t('stocksPage.intraday.high')}
          <span className="ml-1.5 font-mono text-foreground/80">
            {formatWan(Math.max(...points.map(p => p.market_value)), locale)}
          </span>
        </span>
        <span>
          {t('stocksPage.intraday.low')}
          <span className="ml-1.5 font-mono text-foreground/80">
            {formatWan(Math.min(...points.map(p => p.market_value)), locale)}
          </span>
        </span>
      </div>

      <div
        className="relative"
        onPointerMove={handlePointer}
        onPointerLeave={() => setHoverIndex(null)}
      >
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="w-full h-auto select-none"
          role="img"
          aria-label={t('stocksPage.intraday.title')}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={fill} />
              <stop offset="100%" stopColor={marketColorWithAlpha(stroke, 0)} />
            </linearGradient>
          </defs>

          {geometry.ticks.map(tick => (
            <g key={tick.value}>
              <line
                x1={PAD.left}
                x2={WIDTH - PAD.right}
                y1={tick.y}
                y2={tick.y}
                stroke="hsl(var(--border))"
                strokeWidth={0.5}
                strokeDasharray="2 4"
              />
              <text
                x={PAD.left - 8}
                y={tick.y + 3.5}
                textAnchor="end"
                fill="hsl(var(--muted-foreground))"
                fontSize={10}
              >
                {formatWan(tick.value, locale)}
              </text>
            </g>
          ))}

          {/* 昨收市值基准线 */}
          <line
            x1={PAD.left}
            x2={WIDTH - PAD.right}
            y1={geometry.yOf(baseline)}
            y2={geometry.yOf(baseline)}
            stroke="hsl(var(--muted-foreground))"
            strokeWidth={1}
            strokeDasharray="4 4"
            opacity={0.55}
          />
          <text
            x={WIDTH - PAD.right}
            y={geometry.yOf(baseline) - 5}
            textAnchor="end"
            fill="hsl(var(--muted-foreground))"
            fontSize={10}
          >
            {t('stocksPage.intraday.baseline')} {formatWan(baseline, locale)}
          </text>

          <path d={geometry.area} fill={`url(#${gradientId})`} stroke="none" />
          <path
            d={geometry.line}
            fill="none"
            stroke={stroke}
            strokeWidth={1.8}
            strokeLinecap="round"
            strokeLinejoin="round"
          />

          {geometry.labelIdx.map(i => (
            <text
              key={i}
              x={geometry.coords[i].x}
              y={HEIGHT - 8}
              textAnchor={i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle'}
              fill="hsl(var(--muted-foreground))"
              fontSize={10}
            >
              {points[i].time}
            </text>
          ))}

          {activeCoord && (
            <>
              <line
                x1={activeCoord.x}
                x2={activeCoord.x}
                y1={PAD.top}
                y2={PAD.top + (HEIGHT - PAD.top - PAD.bottom)}
                stroke="hsl(var(--muted-foreground))"
                strokeWidth={0.8}
                strokeDasharray="3 3"
                opacity={0.6}
              />
              <circle cx={activeCoord.x} cy={activeCoord.y} r={3.2} fill={stroke} />
            </>
          )}
        </svg>

        {active && activeCoord && (
          <div
            className="pointer-events-none absolute z-10 min-w-[132px] rounded-md border border-border bg-popover px-2.5 py-1.5 shadow-sm"
            style={{
              left: tooltipLeft,
              top: 0,
              transform: flipTooltip ? 'translateX(-100%)' : 'translateX(0)',
            }}
          >
            <div className="font-mono text-[11px] text-muted-foreground">{active.time}</div>
            <div className="font-mono text-[13px] font-semibold text-foreground">
              {formatMoney(active.market_value, locale)}
            </div>
            <div className={`font-mono text-[11px] ${marketSignTextClass(activeChange)}`}>
              {activeChange >= 0 ? '+' : ''}
              {formatMoney(activeChange, locale)}
              {baseline > 0 && (
                <span className="ml-1">
                  ({activeChange >= 0 ? '+' : ''}
                  {((activeChange / baseline) * 100).toFixed(2)}%)
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
