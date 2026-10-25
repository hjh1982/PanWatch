import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Newspaper, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'

interface NewsItem {
  source: string
  source_label: string
  external_id: string
  title: string
  content: string
  publish_time: string
  symbols: string[]
  importance: number
  url: string
}

interface CalendarEvent {
  date: string
  type: 'dividend_record' | 'dividend_ex' | 'lift' | 'disclosure' | 'deadline'
  symbol: string
  name: string
  title: string
  detail: string
}

const ANNOUNCEMENTS_PAGE_SIZE = 20

const weekdayKeys = [
  'stocksPage.messages.weekdaySun',
  'stocksPage.messages.weekdayMon',
  'stocksPage.messages.weekdayTue',
  'stocksPage.messages.weekdayWed',
  'stocksPage.messages.weekdayThu',
  'stocksPage.messages.weekdayFri',
  'stocksPage.messages.weekdaySat',
]

export function HoldingAnnouncementsCard({ names, ready = true }: { names: string[]; ready?: boolean }) {
  const { t } = useTranslation('configuration')
  const stockT = t as unknown as (key: string, options?: Record<string, unknown>) => string
  const [items, setItems] = useState<NewsItem[]>([])
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const requestId = useRef(0)
  const namesKey = useMemo(() => names.join(','), [names])

  const load = useCallback(async () => {
    const id = ++requestId.current
    const list = namesKey.split(',').filter(Boolean)
    if (!list.length) {
      setItems([])
      setPage(1)
      return
    }
    setLoading(true)
    try {
      const params = new URLSearchParams({
        names: list.join(','),
        hours: '720',
        limit: '200',
        source: 'eastmoney',
        filter_related: 'true',
      })
      const data = await fetchAPI<NewsItem[]>(`/news?${params}`)
      if (id !== requestId.current) return
      setItems(data || [])
      setPage(1)
    } catch (e) {
      console.error('加载持仓公告失败:', e)
    } finally {
      if (id === requestId.current) setLoading(false)
    }
  }, [namesKey])

  useEffect(() => { void load() }, [load])

  const groups = useMemo(() => {
    const groupMap = new Map<string, { date: string; stock: string; items: NewsItem[]; latest: number }>()
    for (const item of items) {
      const d = item.publish_time ? new Date(item.publish_time) : null
      if (!d || isNaN(d.getTime())) continue
      const date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
      const stock = item.symbols?.[0] || item.title.split(/[:：]/)[0] || ''
      const key = `${date}__${stock}`
      const g = groupMap.get(key)
      if (g) {
        g.items.push(item)
        if (d.getTime() > g.latest) g.latest = d.getTime()
      } else {
        groupMap.set(key, { date, stock, items: [item], latest: d.getTime() })
      }
    }
    return Array.from(groupMap.values())
      .sort((a, b) => b.latest - a.latest || (a.stock > b.stock ? 1 : -1))
      .map(g => ({ ...g, items: g.items.sort((a, b) => new Date(b.publish_time).getTime() - new Date(a.publish_time).getTime()) }))
  }, [items])

  const pages = useMemo(() => {
    const out: typeof groups[] = []
    let current: typeof groups = []
    let count = 0
    for (const g of groups) {
      if (current.length > 0 && count + g.items.length > ANNOUNCEMENTS_PAGE_SIZE) {
        out.push(current)
        current = []
        count = 0
      }
      current.push(g)
      count += g.items.length
    }
    if (current.length) out.push(current)
    return out
  }, [groups])

  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center gap-2">
        <Newspaper className="h-4 w-4 text-primary" />
        <h2 className="text-[14px] font-semibold">{stockT('stocksPage.messages.holdingAnnouncements')}</h2>
        <span className="text-[11px] text-muted-foreground">
          {stockT('stocksPage.messages.positionCount', { count: items.length })}
        </span>
        <Button
          variant="ghost" size="sm" className="ml-auto h-7 text-[12px]"
          onClick={() => void load()}
          disabled={loading || !ready}
        >
          {loading ? (
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-current/30 border-t-current" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
        </Button>
      </div>

      {!ready || (loading && items.length === 0) ? (
        <div className="py-8 text-center text-[12px] text-muted-foreground">{stockT('stocksPage.messages.loading')}</div>
      ) : groups.length === 0 ? (
        <div className="py-8 text-center text-[12px] text-muted-foreground">{stockT('stocksPage.messages.noHoldingAnnouncements')}</div>
      ) : (
        <>
          <div className="scrollbar max-h-[320px] space-y-4 overflow-y-auto pr-1">
            {pages[page - 1]?.map(g => (
              <div key={`${g.date}__${g.stock}`}>
                <div className="mb-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                  <span className="font-mono">{g.date}</span>
                  <span className="rounded bg-accent/60 px-1.5 py-0.5 text-[11px] text-foreground">{g.stock}</span>
                  <span className="text-[10px] opacity-70">{g.items.length}</span>
                </div>
                <div className="space-y-1.5 pl-1">
                  {g.items.map(it => {
                    const d = new Date(it.publish_time)
                    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
                    const title = it.title.replace(/^[^:：]+[:：]/, '')
                    return (
                      <a
                        key={it.external_id}
                        href={it.url || '#'}
                        target="_blank"
                        rel="noreferrer"
                        className="group flex items-start gap-2 text-[12px]"
                      >
                        <span className="shrink-0 font-mono text-[11px] text-muted-foreground">{hm === '00:00' ? '' : hm}</span>
                        <span className="text-foreground group-hover:text-primary group-hover:underline">{title}</span>
                      </a>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
          {pages.length > 1 && (
            <div className="mt-4 flex items-center justify-center gap-3 text-[12px]">
              <Button
                variant="outline" size="sm" className="h-7"
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page <= 1}
              >
                {stockT('stocksPage.messages.prevPage')}
              </Button>
              <span className="font-mono text-muted-foreground">
                {page} / {pages.length}
              </span>
              <Button
                variant="outline" size="sm" className="h-7"
                onClick={() => setPage(p => Math.min(pages.length, p + 1))}
                disabled={page >= pages.length}
              >
                {stockT('stocksPage.messages.nextPage')}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

export function PortfolioCalendarCard() {
  const { t } = useTranslation('configuration')
  const stockT = t as unknown as (key: string, options?: Record<string, unknown>) => string
  const [events, setEvents] = useState<CalendarEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState<'all' | 'dividend' | 'lift' | 'deadline'>('all')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetchAPI('/portfolio/calendar') as { events?: CalendarEvent[] }
      setEvents(res?.events || [])
    } catch (e) {
      console.error('Failed to load portfolio calendar:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const filtered = useMemo(() => events.filter(e => {
    if (filter === 'all') return true
    if (filter === 'dividend') return e.type === 'dividend_record' || e.type === 'dividend_ex'
    if (filter === 'deadline') return e.type === 'deadline' || e.type === 'disclosure'
    return e.type === filter
  }), [events, filter])

  const groups = useMemo(() => {
    const out: { date: string; items: CalendarEvent[] }[] = []
    for (const e of filtered) {
      const g = out.find(x => x.date === e.date)
      if (g) g.items.push(e)
      else out.push({ date: e.date, items: [e] })
    }
    return out
  }, [filtered])

  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-[14px] font-semibold">{stockT('stocksPage.messages.eventCalendar')}</h2>
        <span className="text-[11px] text-muted-foreground">
          {stockT('stocksPage.messages.positionCount', { count: events.length })}
        </span>
        <Button
          variant="ghost" size="sm" className="ml-auto h-7 text-[12px]"
          onClick={() => void load()}
          disabled={loading}
        >
          {loading ? (
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-current/30 border-t-current" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
        </Button>
      </div>

      <div className="mb-3 flex items-center gap-1">
        {[
          { value: 'all', label: stockT('stocksPage.messages.calendarFilterAll') },
          { value: 'dividend', label: stockT('stocksPage.messages.calendarFilterDividend') },
          { value: 'lift', label: stockT('stocksPage.messages.calendarFilterLift') },
          { value: 'deadline', label: stockT('stocksPage.messages.calendarFilterDeadline') },
        ].map(opt => (
          <button
            key={opt.value}
            onClick={() => setFilter(opt.value as typeof filter)}
            className={`text-[11px] px-2 py-0.5 rounded transition-colors ${
              filter === opt.value
                ? 'bg-primary text-primary-foreground'
                : 'bg-accent/50 text-muted-foreground hover:bg-accent'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {loading && events.length === 0 ? (
        <div className="py-8 text-center text-[12px] text-muted-foreground">{stockT('stocksPage.messages.loading')}</div>
      ) : groups.length === 0 ? (
        <div className="py-8 text-center text-[12px] text-muted-foreground">{stockT('stocksPage.messages.noCalendarEvents')}</div>
      ) : (
        <div className="scrollbar max-h-[320px] space-y-4 overflow-y-auto pr-1">
          {groups.map(g => {
            const d = new Date(`${g.date}T00:00:00`)
            const weekday = stockT(weekdayKeys[d.getDay()])
            return (
              <div key={g.date}>
                <div className="mb-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                  <span className="font-mono">{g.date}</span>
                  <span>{weekday}</span>
                </div>
                <div className="space-y-1.5 pl-1">
                  {g.items.map((e, i) => {
                    const tagClass = e.type === 'lift'
                      ? 'bg-amber-500/10 text-amber-600'
                      : e.type === 'disclosure'
                      ? 'bg-indigo-500/10 text-indigo-600'
                      : e.type === 'deadline'
                      ? 'bg-blue-500/10 text-blue-600'
                      : e.type === 'dividend_record'
                      ? 'bg-emerald-500/10 text-emerald-600'
                      : 'bg-teal-500/10 text-teal-600'
                    return (
                      <div key={`${e.type}_${e.symbol}_${i}`} className="flex items-start gap-2 text-[12px]">
                        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${tagClass}`}>{e.title}</span>
                        {e.symbol && <span className="shrink-0 text-foreground">{e.name}</span>}
                        {e.detail && <span className="text-muted-foreground">{e.detail}</span>}
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
