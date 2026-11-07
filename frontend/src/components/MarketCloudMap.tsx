import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { LayoutGrid } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { MarketMapItem, MarketMapSnapshot } from '@panwatch/api'
import { discoveryApi } from '@panwatch/api'
import { useMarketColors } from '@/hooks/use-market-colors'
import { squarifyGroups } from '@/lib/treemap'
import type { MarketColorPalette, MarketTone } from '@/lib/market-colors'

/** 涨跌幅达到该值即用最深色，避免少数极端行情吃掉整条色阶。 */
const FULL_COLOR_PCT = 4
const PANEL_BG = '#141414'
const FLAT_TILE_BG = '#262626'
const HEADER_HEIGHT = 15
const GROUP_GAP = 2
const TOOLTIP_WIDTH = 304
const TOOLTIP_MARGIN = 10
/** 面板固定区(板块头+个股摘要+列表头)与上下留白的高度，其余全部留给个股列表。 */
const TOOLTIP_CHROME_HEIGHT = 176

interface Props {
  data: MarketMapSnapshot | null
  loading: boolean
  error: boolean
  onSelectStock: (symbol: string, market: string, name: string) => void
}

interface IndustryGroup {
  key: string
  value: number
  change: number | null
  up: number
  flat: number
  down: number
  items: MarketMapItem[]
}

function formatPct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}

function formatPrice(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return value.toFixed(2)
}

function formatDelta(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}`
}

function changeOf(item: MarketMapItem): number {
  const value = Number(item.change_pct)
  return Number.isFinite(value) ? value : Number.NaN
}

function capOf(item: MarketMapItem): number {
  const value = Number(item.total_market_value)
  return Number.isFinite(value) && value > 0 ? value : 0
}

function hexToRgb(hex: string): [number, number, number] | null {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex)
  if (!match) return null
  const n = Number.parseInt(match[1], 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

function mixHex(from: string, to: string, ratio: number): string {
  const a = hexToRgb(from)
  const b = hexToRgb(to)
  if (!a || !b) return to
  const channel = (x: number, y: number) => Math.round(x + (y - x) * ratio)
  return `rgb(${channel(a[0], b[0])}, ${channel(a[1], b[1])}, ${channel(a[2], b[2])})`
}

/** 深色底上用暗红/暗绿铺色,涨跌越深颜色越亮。 */
function tileColors(pct: number, palette: MarketColorPalette): { background: string; color: string } {
  if (!Number.isFinite(pct) || Math.abs(pct) < 0.05) {
    return { background: FLAT_TILE_BG, color: 'rgba(235, 235, 235, 0.55)' }
  }
  const tone: MarketTone = pct > 0 ? palette.up : palette.down
  const strength = Math.min(Math.abs(pct) / FULL_COLOR_PCT, 1)
  return { background: mixHex(PANEL_BG, tone.bright, 0.32 + strength * 0.6), color: '#f5f5f5' }
}

function groupChange(items: MarketMapItem[]): number | null {
  let weighted = 0
  let weight = 0
  for (const item of items) {
    const pct = changeOf(item)
    const cap = capOf(item)
    if (!Number.isFinite(pct) || cap <= 0) continue
    weighted += pct * cap
    weight += cap
  }
  return weight > 0 ? weighted / weight : null
}

export default function MarketCloudMap({ data, loading, error, onSelectStock }: Props) {
  const { t, i18n } = useTranslation('configuration')
  const tr = t as unknown as (key: string, options?: Record<string, unknown>) => string
  const { palette } = useMarketColors()
  const containerRef = useRef<HTMLDivElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const pointerRef = useRef({ x: 0, y: 0 })
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const sectorCacheRef = useRef(new Map<string, MarketMapItem[]>())
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [viewport, setViewport] = useState(() => ({ width: window.innerWidth, height: window.innerHeight }))
  const [hoverItem, setHoverItem] = useState<MarketMapItem | null>(null)
  const [sectorItems, setSectorItems] = useState<MarketMapItem[] | null>(null)
  const [sectorLoading, setSectorLoading] = useState(false)
  const [sortDesc, setSortDesc] = useState(true)
  const listRef = useRef<HTMLDivElement>(null)

  const cancelHide = () => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current)
      hideTimerRef.current = null
    }
  }

  const scheduleHide = () => {
    cancelHide()
    hideTimerRef.current = setTimeout(() => setHoverItem(null), 140)
  }

  useEffect(() => cancelHide, [])

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const measure = () => setSize({ width: el.clientWidth, height: el.clientHeight })
    measure()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  const trackPointer = (event: ReactMouseEvent<HTMLElement>) => {
    pointerRef.current = { x: event.clientX, y: event.clientY }
  }

  const placeTooltip = () => {
    const tip = tooltipRef.current
    if (!tip) return
    const width = tip.offsetWidth || TOOLTIP_WIDTH
    const height = tip.offsetHeight || 110
    const { x, y } = pointerRef.current
    const left = Math.min(
      Math.max(TOOLTIP_MARGIN, x + 16),
      Math.max(TOOLTIP_MARGIN, window.innerWidth - width - TOOLTIP_MARGIN),
    )
    const top = y + height + 20 > window.innerHeight
      ? Math.max(TOOLTIP_MARGIN, y - height - 14)
      : y + 16
    tip.style.transform = `translate3d(${left}px, ${top}px, 0)`
  }

  useLayoutEffect(() => {
    if (hoverItem) placeTooltip()
  }, [hoverItem, sectorItems, sectorLoading, viewport])

  useEffect(() => {
    const onResize = () => setViewport({ width: window.innerWidth, height: window.innerHeight })
    const onScroll = () => setHoverItem(null)
    window.addEventListener('resize', onResize)
    window.addEventListener('scroll', onScroll)
    return () => {
      window.removeEventListener('resize', onResize)
      window.removeEventListener('scroll', onScroll)
    }
  }, [])

  const items = data?.items || []
  const stats = data?.stats
  const language = i18n.resolvedLanguage || i18n.language
  const otherLabel = tr('dashboard.marketCloud.other')

  const formatCompact = (value: number | null | undefined): string => {
    if (value == null || !Number.isFinite(value) || value <= 0) return '--'
    return new Intl.NumberFormat(language, { notation: 'compact', maximumFractionDigits: 2 }).format(value)
  }

  const formatTime = (value: number | null | undefined): string => {
    if (!value) return '--'
    return new Intl.DateTimeFormat(language, {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    }).format(new Date(value * 1000))
  }

  const groups = useMemo<IndustryGroup[]>(() => {
    const buckets = new Map<string, MarketMapItem[]>()
    for (const item of items) {
      if (capOf(item) <= 0) continue
      const key = item.industry?.trim() || otherLabel
      const bucket = buckets.get(key)
      if (bucket) bucket.push(item)
      else buckets.set(key, [item])
    }
    return [...buckets.entries()]
      .map(([key, list]) => ({
        key,
        value: list.reduce((sum, item) => sum + capOf(item), 0),
        change: groupChange(list),
        up: list.filter((item) => changeOf(item) > 0.05).length,
        flat: list.filter((item) => Math.abs(changeOf(item)) <= 0.05).length,
        down: list.filter((item) => changeOf(item) < -0.05).length,
        items: list.slice().sort((a, b) => capOf(b) - capOf(a)),
      }))
      .sort((a, b) => b.value - a.value)
  }, [items, otherLabel])

  const layout = useMemo(
    () =>
      squarifyGroups(
        groups.map((group) => ({
          key: group.key,
          value: group.value,
          entries: group.items.map((item) => ({ key: item.symbol, value: capOf(item) })),
        })),
        size.width,
        size.height,
        { gap: GROUP_GAP, headerHeight: HEADER_HEIGHT },
      ),
    [groups, size.width, size.height],
  )

  const bySymbol = useMemo(() => new Map(items.map((item) => [item.symbol, item])), [items])
  const groupMeta = useMemo(() => new Map(groups.map((group) => [group.key, group])), [groups])
  const hoverGroup = hoverItem
    ? groupMeta.get(hoverItem.industry?.trim() || otherLabel) || null
    : null
  const hoverSectorKey = hoverGroup?.key || null

  // 悬停板块的全量成分股（本页云图只含市值前 N 只，这里补全整个板块）。
  useEffect(() => {
    if (!hoverSectorKey || hoverSectorKey === otherLabel) {
      setSectorItems(null)
      setSectorLoading(false)
      return
    }
    const cached = sectorCacheRef.current.get(hoverSectorKey)
    if (cached) {
      setSectorItems(cached)
      setSectorLoading(false)
      return
    }
    let cancelled = false
    setSectorItems(null)
    setSectorLoading(true)
    discoveryApi
      .marketMapSector({ industry: hoverSectorKey, market: 'CN' })
      .then((res) => {
        const list = res?.items?.length ? res.items : null
        if (list) sectorCacheRef.current.set(hoverSectorKey, list)
        if (!cancelled) setSectorItems(list)
      })
      .catch(() => {
        if (!cancelled) setSectorItems(null)
      })
      .finally(() => {
        if (!cancelled) setSectorLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [hoverSectorKey, otherLabel])

  const sectorList = sectorItems?.length ? sectorItems : hoverGroup?.items || []
  const sortedSectorList = useMemo(() => {
    return sectorList.slice().sort((a, b) => {
      const av = changeOf(a)
      const bv = changeOf(b)
      const aOk = Number.isFinite(av)
      const bOk = Number.isFinite(bv)
      if (aOk !== bOk) return aOk ? -1 : 1
      if (!aOk) return capOf(b) - capOf(a)
      const diff = sortDesc ? bv - av : av - bv
      return diff !== 0 ? diff : capOf(b) - capOf(a)
    })
  }, [sectorList, sortDesc])
  useLayoutEffect(() => {
    if (listRef.current) listRef.current.scrollTop = 0
  }, [hoverSectorKey, sortDesc])
  const sectorStats = useMemo(() => {
    if (!sectorItems?.length) return null
    let up = 0
    let flat = 0
    let down = 0
    for (const item of sectorItems) {
      const pct = changeOf(item)
      if (!Number.isFinite(pct) || Math.abs(pct) <= 0.05) flat += 1
      else if (pct > 0) up += 1
      else down += 1
    }
    return { up, flat, down, change: groupChange(sectorItems) }
  }, [sectorItems])
  const sectorUp = sectorStats?.up ?? hoverGroup?.up ?? 0
  const sectorFlat = sectorStats?.flat ?? hoverGroup?.flat ?? 0
  const sectorDown = sectorStats?.down ?? hoverGroup?.down ?? 0
  const sectorChange = sectorStats?.change ?? hoverGroup?.change ?? null
  const tooltipWidth = Math.max(260, Math.min(TOOLTIP_WIDTH, viewport.width - TOOLTIP_MARGIN * 2))
  const tooltipListHeight = Math.max(120, viewport.height - TOOLTIP_CHROME_HEIGHT)
  const tilesByGroup = useMemo(() => {
    const map = new Map<string, typeof layout.tiles>()
    for (const tile of layout.tiles) {
      const list = map.get(tile.group)
      if (list) list.push(tile)
      else map.set(tile.group, [tile])
    }
    return map
  }, [layout.tiles])

  const changeColor = (change: number | null | undefined) =>
    change == null || !Number.isFinite(change)
      ? '#9a9a9a'
      : change > 0
        ? palette.up.bright
        : palette.down.bright

  return (
    <div className="relative mb-3 overflow-hidden rounded-xl border border-white/[0.06] bg-[#141414] text-[#e8e8e8] shadow-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-white/[0.06] px-3 py-2 md:px-4">
        <div className="flex min-w-0 items-center gap-1.5">
          <LayoutGrid className="h-4 w-4 text-[#ff8a3d]" />
          <h2 className="text-sm font-semibold">{tr('dashboard.marketCloud.title')}</h2>
          <span className="truncate text-[11px] text-[#8f8f8f]">{tr('dashboard.marketCloud.hint')}</span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] text-[#9a9a9a]">
          {stats && (
            <>
              <span>
                {tr('dashboard.marketCloud.up')}{' '}
                <b style={{ color: palette.up.bright }}>{stats.up}</b>
              </span>
              <span>
                {tr('dashboard.marketCloud.flat')} <b className="text-[#cfcfcf]">{stats.flat}</b>
              </span>
              <span>
                {tr('dashboard.marketCloud.down')}{' '}
                <b style={{ color: palette.down.bright }}>{stats.down}</b>
              </span>
              <span className="hidden sm:inline">
                {tr('dashboard.marketCloud.amount')} <b className="text-[#d6d6d6]">{formatCompact(stats.amount)}</b>
              </span>
            </>
          )}
          <span>
            {tr('dashboard.marketCloud.coverage', {
              count: items.length,
              total: stats?.total ?? items.length,
            })}
          </span>
        </div>
      </div>

      <div ref={containerRef} className="relative h-[300px] w-full md:h-[420px]">
        {items.length === 0 ? (
          <div className="flex h-full items-center justify-center px-4 text-center text-[12px] text-[#8f8f8f]">
            {loading
              ? tr('dashboard.marketCloud.loading')
              : error
                ? tr('dashboard.marketCloud.unavailable')
                : tr('dashboard.marketCloud.empty')}
          </div>
        ) : (
          layout.groups.map((rect) => {
            const meta = groupMeta.get(rect.key)
            const tiles = tilesByGroup.get(rect.key) || []
            const active = hoverGroup?.key === rect.key
            const showPct = rect.width >= 96
            const showBreadth = rect.width >= 150
            return (
              <div
                key={rect.key}
                className="absolute overflow-hidden bg-white/[0.02]"
                style={{
                  left: rect.x,
                  top: rect.y,
                  width: rect.width,
                  height: rect.height,
                  boxShadow: active
                    ? 'inset 0 0 0 2px #ffd614'
                    : 'inset 0 0 0 1px rgba(255, 255, 255, 0.05)',
                }}
              >
                {rect.headerHeight > 0 && (
                  <div
                    className={`flex items-center gap-1 overflow-hidden border-b border-white/[0.06] px-1 ${
                      active ? 'bg-[rgba(255,233,134,0.16)]' : 'bg-white/[0.05]'
                    }`}
                    style={{ height: rect.headerHeight }}
                  >
                    <span className="min-w-0 flex-1 truncate text-[10px] font-medium text-[#dcdcdc]">{rect.key}</span>
                    {showBreadth && (
                      <span className="shrink-0 font-mono text-[9px] text-[#8a8a8a]">
                        {tr('dashboard.marketCloud.groupBreadth', { up: meta?.up ?? 0, down: meta?.down ?? 0 })}
                      </span>
                    )}
                    {showPct && (
                      <span className="shrink-0 font-mono text-[10px]" style={{ color: changeColor(meta?.change) }}>
                        {formatPct(meta?.change)}
                      </span>
                    )}
                  </div>
                )}

                {tiles.map((tile) => {
                  const item = bySymbol.get(tile.key)
                  if (!item) return null
                  const pct = changeOf(item)
                  const colors = tileColors(pct, palette)
                  const width = Math.max(1, tile.width - 1)
                  const height = Math.max(1, tile.height - 1)
                  const showName = width >= 40 && height >= 16
                  const showTilePct = width >= 44 && height >= 28
                  const roomy = width >= 110 && height >= 60
                  const medium = width >= 72 && height >= 34
                  return (
                    <button
                      key={tile.key}
                      type="button"
                      onClick={() => onSelectStock(item.symbol, item.market || 'CN', item.name)}
                      onMouseEnter={(event) => {
                        cancelHide()
                        trackPointer(event)
                        setHoverItem(item)
                      }}
                      onMouseMove={(event) => {
                        trackPointer(event)
                        placeTooltip()
                      }}
                      onMouseLeave={scheduleHide}
                      aria-label={`${item.name} ${formatPct(pct)}`}
                      className="absolute overflow-hidden px-1 text-left transition-[filter] hover:z-10 hover:brightness-125 focus:z-10 focus:outline-none focus-visible:ring-1 focus-visible:ring-[#ff8a3d]"
                      style={{
                        left: tile.x - rect.x,
                        top: tile.y - rect.y,
                        width,
                        height,
                        backgroundColor: colors.background,
                        color: colors.color,
                        outline: active ? '1px solid rgba(255, 214, 20, 0.85)' : undefined,
                        outlineOffset: active ? -1 : undefined,
                      }}
                    >
                      {showName && (
                        <span
                          className="block truncate font-medium leading-tight"
                          style={{ fontSize: roomy ? 13 : medium ? 11 : 9.5 }}
                        >
                          {item.name}
                        </span>
                      )}
                      {showTilePct && (
                        <span
                          className="block truncate font-mono leading-tight opacity-80"
                          style={{ fontSize: roomy ? 11 : medium ? 9.5 : 8.5 }}
                        >
                          {formatPct(pct)}
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            )
          })
        )}

        {hoverItem && hoverGroup && createPortal(
          <div
            ref={tooltipRef}
            role="tooltip"
            onMouseEnter={cancelHide}
            onMouseLeave={scheduleHide}
            className="scrollbar pointer-events-auto fixed left-0 top-0 z-[70] overflow-hidden rounded-xl border border-white/10 bg-[#1c1c1c] text-[#e8e8e8] shadow-2xl ring-1 ring-black/50"
            style={{ width: tooltipWidth }}
          >
            <div className="flex items-center justify-between gap-2 border-b border-white/10 bg-white/[0.04] px-3 py-2">
              <span className="flex min-w-0 items-center gap-1.5">
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ backgroundColor: changeColor(sectorChange) }}
                />
                <span className="truncate text-[13px] font-semibold">{hoverGroup.key}</span>
                <span className="shrink-0 rounded bg-white/[0.07] px-1 py-0.5 font-mono text-[10px] text-[#a8a8a8]">
                  {sectorList.length}
                </span>
              </span>
              <span className="shrink-0 font-mono text-[12px] font-semibold" style={{ color: changeColor(sectorChange) }}>
                {formatPct(sectorChange)}
              </span>
            </div>

            <div
              className="border-l-2 bg-white/[0.03] px-3 py-2.5"
              style={{ borderColor: changeColor(changeOf(hoverItem)) }}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-[14px] font-semibold text-[#f2f2f2]">{hoverItem.name}</span>
                <span className="shrink-0 font-mono text-[11px] text-[#8f8f8f]">{hoverItem.symbol}</span>
              </div>
              <div className="mt-1 flex items-baseline gap-2 font-mono">
                <span className="text-[20px] leading-none text-[#f5f5f5]">{formatPrice(hoverItem.price)}</span>
                <span className="text-[12px]" style={{ color: changeColor(changeOf(hoverItem)) }}>
                  {formatDelta(hoverItem.change_amount)}
                </span>
                <span className="text-[13px] font-semibold" style={{ color: changeColor(changeOf(hoverItem)) }}>
                  {formatPct(changeOf(hoverItem))}
                </span>
              </div>
              <div className="mt-1.5 flex items-center gap-4 text-[11px] text-[#9a9a9a]">
                <span>
                  {tr('dashboard.marketCloud.up')}{' '}
                  <b className="font-mono" style={{ color: palette.up.bright }}>{sectorUp}</b>
                </span>
                <span>
                  {tr('dashboard.marketCloud.flat')} <b className="font-mono text-[#cfcfcf]">{sectorFlat}</b>
                </span>
                <span>
                  {tr('dashboard.marketCloud.down')}{' '}
                  <b className="font-mono" style={{ color: palette.down.bright }}>{sectorDown}</b>
                </span>
              </div>
            </div>

            <div className="border-t border-white/10">
              <div className="flex items-center justify-between px-3 pb-1 pt-1.5 text-[11px] text-[#8f8f8f]">
                <span className="flex items-center gap-1">
                  {tr('dashboard.marketCloud.sectorStocks')}
                  {sectorLoading && (
                    <span className="h-1 w-1 animate-pulse rounded-full bg-[#ff8a3d]" />
                  )}
                </span>
                <span className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setSortDesc((value) => !value)}
                    aria-label={`${tr('dashboard.marketCloud.change')} ${sortDesc ? '↓' : '↑'}`}
                    className="flex items-center gap-0.5 rounded px-1 py-0.5 font-mono transition-colors hover:bg-white/10"
                    style={{ color: sortDesc ? palette.up.bright : palette.down.bright }}
                  >
                    {tr('dashboard.marketCloud.change')}
                    <span aria-hidden="true">{sortDesc ? '↓' : '↑'}</span>
                  </button>
                  <span className="font-mono">{sortedSectorList.length}</span>
                </span>
              </div>
              <div
                ref={listRef}
                className="scrollbar overflow-y-auto overflow-x-hidden pb-1"
                style={{ maxHeight: tooltipListHeight }}
              >
                {sortedSectorList.map((row) => {
                  const rowPct = changeOf(row)
                  const isCurrent = row.symbol === hoverItem.symbol
                  return (
                    <button
                      key={row.symbol}
                      type="button"
                      onClick={() => onSelectStock(row.symbol, row.market || 'CN', row.name)}
                      className={`flex w-full items-center gap-2 px-3 py-[3px] text-left text-[11px] transition-colors hover:bg-white/10 ${
                        isCurrent ? 'bg-white/[0.08]' : ''
                      }`}
                    >
                      <span className="min-w-0 flex-1 truncate text-[#e0e0e0]">{row.name}</span>
                      <span className="w-14 shrink-0 text-right font-mono text-[#b8b8b8]">
                        {formatPrice(row.price)}
                      </span>
                      <span className="w-14 shrink-0 text-right font-mono" style={{ color: changeColor(rowPct) }}>
                        {formatPct(rowPct)}
                      </span>
                    </button>
                  )
                })}
              </div>
            </div>
          </div>,
          document.body,
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-white/[0.06] px-3 py-1.5 text-[10px] text-[#8f8f8f] md:px-4">
        <span>{tr('dashboard.marketCloud.mapHint')}</span>
        <span className="ml-auto flex items-center gap-1.5">
          <span className="font-mono">-{FULL_COLOR_PCT}%</span>
          <span
            className="h-2 w-24 rounded-full md:w-32"
            style={{
              background: `linear-gradient(90deg, ${palette.down.bright}, #3a3a3a, ${palette.up.bright})`,
            }}
          />
          <span className="font-mono">+{FULL_COLOR_PCT}%</span>
        </span>
        {stats?.as_of ? (
          <span className="font-mono">{tr('dashboard.marketCloud.asOf', { time: formatTime(stats.as_of) })}</span>
        ) : null}
      </div>
    </div>
  )
}