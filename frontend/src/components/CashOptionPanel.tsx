import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, ExternalLink, RefreshCw } from 'lucide-react'
import { recommendationsApi, type CashOptionEvent, type CashOptionPayload } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useTranslation } from 'react-i18next'

type Translate = (key: string, options?: Record<string, unknown>) => string

const statusTone: Record<CashOptionEvent['status'], string> = {
  active: 'bg-market-up/15 text-market-up border-market-up/30',
  upcoming: 'bg-sky-500/15 text-sky-400 border-sky-500/30',
  ended: 'bg-muted text-muted-foreground border-border/60',
  unknown: 'bg-amber-500/12 text-amber-300 border-amber-500/25',
}

const formatPrice = (value: number | null) =>
  typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '--'

const formatPct = (value: number | null) => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}

const premiumTone = (value: number | null) => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value === 0) return 'text-muted-foreground'
  return value > 0 ? 'text-market-up' : 'text-market-down'
}

function CashOptionCard({ event }: { event: CashOptionEvent }) {
  const { t } = useTranslation('configuration')
  const tr = t as unknown as Translate
  const basisLabel = event.basis
    ? tr(`opportunities.cashOption.basis.${event.basis}`)
    : ''
  const basisPrice = event.basis === 'reference' ? event.reference_price : event.quote_price
  const ended = event.status === 'ended'

  return (
    <div className={`card p-4 ${ended ? 'opacity-80' : ''}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="text-[15px] font-semibold">{event.name}</span>
            <span className="text-[11px] font-mono text-muted-foreground">
              {event.market}:{event.code}
            </span>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            <span className={`rounded border px-1.5 py-0.5 text-[10px] ${statusTone[event.status]}`}>
              {tr(`opportunities.cashOption.status.${event.status}`)}
            </span>
            <span className="rounded border border-border/60 bg-accent/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {tr(`opportunities.cashOption.method.${event.method}`)}
            </span>
            <span className="rounded border border-border/60 bg-accent/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {tr(`opportunities.cashOption.eligibility.${event.eligibility}`)}
            </span>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-[10px] text-muted-foreground">
            {tr('opportunities.cashOption.metrics.premium')}
          </div>
          <div className={`text-[20px] font-bold leading-tight ${premiumTone(event.premium_pct)}`}>
            {formatPct(event.premium_pct)}
          </div>
          {basisLabel && (
            <div className="text-[10px] text-muted-foreground">{basisLabel}</div>
          )}
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <div className="rounded bg-accent/40 px-2 py-1.5">
          <div className="text-[10px] text-muted-foreground">
            {tr('opportunities.cashOption.metrics.exercise')}
          </div>
          <div className="text-[13px] font-semibold">{formatPrice(event.exercise_price)}</div>
        </div>
        <div className="rounded bg-accent/40 px-2 py-1.5">
          <div className="text-[10px] text-muted-foreground">
            {event.basis === 'reference'
              ? tr('opportunities.cashOption.metrics.reference')
              : tr('opportunities.cashOption.metrics.quote')}
          </div>
          <div className="text-[13px] font-semibold">{formatPrice(basisPrice)}</div>
          {event.quote_date && (
            <div className="text-[10px] text-muted-foreground">{event.quote_date}</div>
          )}
        </div>
        <div className="rounded bg-accent/40 px-2 py-1.5">
          <div className="text-[10px] text-muted-foreground">
            {tr('opportunities.cashOption.fields.recordDate')}
          </div>
          <div className="text-[13px] font-semibold">{event.record_date || '--'}</div>
        </div>
        <div className="rounded bg-accent/40 px-2 py-1.5">
          <div className="text-[10px] text-muted-foreground">
            {tr('opportunities.cashOption.fields.window')}
          </div>
          <div className="text-[12px] font-semibold">
            {event.window_start || '--'} ~ {event.window_end || '--'}
          </div>
          {event.days_left != null && event.days_left >= 0 && (
            <div className="text-[10px] text-muted-foreground">
              {tr('opportunities.cashOption.fields.daysLeft', { days: event.days_left })}
            </div>
          )}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] text-muted-foreground">
        {event.annualized_pct != null && (
          <span>
            {tr('opportunities.cashOption.metrics.annualized')}:{' '}
            <span className={premiumTone(event.annualized_pct)}>{formatPct(event.annualized_pct)}</span>
          </span>
        )}
        {event.apply_code && (
          <span>
            {tr('opportunities.cashOption.fields.applyCode')}:{' '}
            <span className="font-mono text-foreground">{event.apply_code}</span>
          </span>
        )}
        <a
          href={event.announcement_url}
          target="_blank"
          rel="noreferrer"
          title={event.announcement_title}
          className="inline-flex min-w-0 items-center gap-1 transition-colors hover:text-primary"
        >
          <ExternalLink className="h-3 w-3 shrink-0" />
          <span className="truncate">
            {tr('opportunities.cashOption.fields.announcement')} {event.announcement_date}
          </span>
        </a>
        {ended && (
          <span className="text-[10px] text-muted-foreground/80">
            {tr('opportunities.cashOption.endedHint')}
          </span>
        )}
      </div>
    </div>
  )
}

export default function CashOptionPanel() {
  const { t } = useTranslation('configuration')
  const tr = t as unknown as Translate
  const [payload, setPayload] = useState<CashOptionPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(
    async (force = false) => {
      if (force) setRefreshing(true)
      else setLoading(true)
      setError('')
      try {
        const data = force
          ? await recommendationsApi.refreshCashOptions()
          : await recommendationsApi.getCashOptions()
        setPayload(data)
      } catch (e) {
        setError(e instanceof Error ? e.message : tr('opportunities.cashOption.fetchFailed'))
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [tr]
  )

  useEffect(() => {
    void load()
  }, [load])

  const summary = payload?.summary
  const events = payload?.events || []
  const fetchFailed = payload?.error === 'fetch_failed'
  const source = payload?.sources?.[0]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-[14px] font-semibold">{tr('opportunities.cashOption.title')}</h2>
          {payload?.as_of && (
            <span className="text-[11px] text-muted-foreground">
              {tr('opportunities.cashOption.asOf', { date: payload.as_of })}
            </span>
          )}
        </div>
        <Button
          variant="secondary"
          size="sm"
          className="h-8 text-[12px]"
          onClick={() => void load(true)}
          disabled={refreshing || loading}
        >
          {refreshing ? (
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current/30 border-t-current" />
          ) : (
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
          )}
          {tr('opportunities.cashOption.refresh')}
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="card p-3">
          <div className="text-[11px] text-muted-foreground">
            {tr('opportunities.cashOption.summary.active')}
          </div>
          <div className="mt-1 text-[18px] font-bold">{summary?.active ?? '--'}</div>
        </div>
        <div className="card p-3">
          <div className="text-[11px] text-muted-foreground">
            {tr('opportunities.cashOption.summary.upcoming')}
          </div>
          <div className="mt-1 text-[18px] font-bold">{summary?.upcoming ?? '--'}</div>
        </div>
        <div className="card p-3">
          <div className="text-[11px] text-muted-foreground">
            {tr('opportunities.cashOption.summary.recentEnded')}
          </div>
          <div className="mt-1 text-[18px] font-bold">{summary?.recent_ended ?? '--'}</div>
        </div>
        <div className="card p-3">
          <div className="text-[11px] text-muted-foreground">
            {tr('opportunities.cashOption.summary.bestPremium')}
          </div>
          <div className={`mt-1 text-[18px] font-bold ${premiumTone(summary?.best_premium_pct ?? null)}`}>
            {formatPct(summary?.best_premium_pct ?? null)}
          </div>
        </div>
      </div>

      {(error || fetchFailed) && (
        <div className="card flex items-center gap-2 p-3 text-[12px] text-amber-500">
          <AlertTriangle className="h-4 w-4" />
          {fetchFailed ? tr('opportunities.cashOption.fetchFailed') : error}
        </div>
      )}

      {loading && events.length === 0 ? (
        <div className="card p-8 text-center text-[12px] text-muted-foreground">
          {tr('opportunities.cashOption.loading')}
        </div>
      ) : events.length === 0 ? (
        <div className="card p-8 text-center text-[12px] text-muted-foreground">
          {tr('opportunities.cashOption.empty')}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {events.map((event) => (
            <CashOptionCard key={event.code} event={event} />
          ))}
        </div>
      )}

      <div className="card p-3">
        <ul className="space-y-1 text-[11px] text-muted-foreground/80">
          {source && (
            <li>
              {tr('opportunities.cashOption.source', {
                name: source.name,
                cadence: source.cadence,
              })}
              {' · '}
              {source.latest || tr('opportunities.cashOption.noLatest')}
            </li>
          )}
          <li>{tr('opportunities.cashOption.caveat')}</li>
        </ul>
      </div>
    </div>
  )
}
