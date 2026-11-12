import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, apiDownload, apiFetch, buildQuery } from '../api'
import type { Lead, LeadStatus, Run, Stats } from '../types'

const PAGE_SIZE = 50

const PERSONAS = ['', 'NEW_LAUNCH', 'STAGNANT_VETERAN', 'DISTRESSED', 'UNKNOWN']
const CONTACT_QUALITIES = ['', 'DIRECT', 'WEBSITE_ONLY', 'UNCONTACTABLE']
const STATUSES: ('' | LeadStatus)[] = ['', 'NEW', 'SHORTLISTED', 'CONTACTED', 'SKIP']
const SORTS = [
  { value: '', label: 'Newest first' },
  { value: 'reviews_asc', label: 'Reviews: low to high' },
  { value: 'reviews_desc', label: 'Reviews: high to low' },
  { value: 'rating_desc', label: 'Rating: high to low' },
]

interface Filters {
  persona: string
  contact_quality: string
  country: string
  max_reviews: string
  q: string
  status: string
  sort: string
}

const EMPTY_FILTERS: Filters = {
  persona: '',
  contact_quality: '',
  country: '',
  max_reviews: '',
  q: '',
  status: '',
  sort: '',
}

function filterParams(f: Filters): Record<string, string | number | undefined> {
  return {
    persona: f.persona || undefined,
    contact_quality: f.contact_quality || undefined,
    country: f.country || undefined,
    max_reviews: f.max_reviews || undefined,
    q: f.q || undefined,
    status: f.status || undefined,
    sort: f.sort || undefined,
  }
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function pct(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return `${Math.round(n * 100)}%`
}

export default function LeadsPage() {
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const [leads, setLeads] = useState<Lead[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [stats, setStats] = useState<Stats | null>(null)
  const [latestRun, setLatestRun] = useState<Run | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [savingId, setSavingId] = useState<number | null>(null)
  const [copiedId, setCopiedId] = useState<number | null>(null)
  const [exportFormat, setExportFormat] = useState<'csv' | 'xlsx'>('csv')
  const [exporting, setExporting] = useState(false)
  const [notesDraft, setNotesDraft] = useState<Record<number, string>>({})

  const loadLeads = useCallback(async (f: Filters, off: number) => {
    setLoading(true)
    setError('')
    try {
      const data = await apiFetch<{ total: number; items: Lead[] }>(
        `/api/leads${buildQuery({ ...filterParams(f), limit: PAGE_SIZE, offset: off })}`,
      )
      setLeads(data.items)
      setTotal(data.total)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load leads.')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadBanner = useCallback(async () => {
    try {
      const [s, runs] = await Promise.all([
        apiFetch<Stats>('/api/stats'),
        apiFetch<Run[]>('/api/runs?limit=1'),
      ])
      setStats(s)
      setLatestRun(Array.isArray(runs) ? runs[0] ?? null : null)
    } catch {
      // Banner is informational; the table shows its own error state.
    }
  }, [])

  useEffect(() => {
    loadBanner()
  }, [loadBanner])

  useEffect(() => {
    loadLeads(filters, offset)
  }, [filters, offset, loadLeads])

  const allSelected = leads.length > 0 && leads.every((l) => selected.has(l.id))
  const selectedCount = useMemo(() => selected.size, [selected])

  function toggleSelect(id: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleSelectAll() {
    setSelected((prev) => {
      if (allSelected) return new Set()
      const next = new Set(prev)
      leads.forEach((l) => next.add(l.id))
      return next
    })
  }

  async function updateLead(id: number, patch: { status?: LeadStatus; notes?: string }) {
    setSavingId(id)
    try {
      await apiFetch(`/api/leads/${id}`, { method: 'PATCH', body: JSON.stringify(patch) })
      setLeads((prev) =>
        prev.map((l) => (l.id === id ? { ...l, status: patch.status ?? l.status } : l)),
      )
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to update lead.')
    } finally {
      setSavingId(null)
    }
  }

  async function copyEmail(lead: Lead) {
    if (!lead.email) return
    try {
      await navigator.clipboard.writeText(lead.email)
    } catch {
      // Fallback for browsers without clipboard permission.
      const ta = document.createElement('textarea')
      ta.value = lead.email
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      ta.remove()
    }
    setCopiedId(lead.id)
    window.setTimeout(() => setCopiedId((c) => (c === lead.id ? null : c)), 1500)
  }

  async function exportFiltered() {
    setExporting(true)
    setError('')
    try {
      // Export uses the CURRENT filters — filtered rows only (v1 bug fix).
      const ext = exportFormat === 'xlsx' ? 'xlsx' : 'csv'
      await apiDownload(
        `/api/leads/export${buildQuery({ format: ext, ...filterParams(filters) })}`,
        `pl-hunter-leads.${ext}`,
      )
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Export failed.')
    } finally {
      setExporting(false)
    }
  }

  const runLine = latestRun
    ? `Latest run #${latestRun.id} — ${latestRun.status} · ${latestRun.leads_new ?? 0} new leads · ${
        latestRun.captcha_blocks ?? 0
      } CAPTCHA blocks · started ${fmtTime(latestRun.started_at)}`
    : 'No runs yet.'

  return (
    <div>
      <section className="banner" aria-label="Run status">
        <div>
          <strong>Daily hunt</strong>
          <p>{runLine}</p>
        </div>
        <div className="banner-stats">
          <span>
            <b>{stats?.leads_new_today ?? '—'}</b> new today
          </span>
          <span>
            <b>{stats?.leads_total ?? '—'}</b> leads total
          </span>
          <span>
            <b>{stats ? pct(stats.captcha_rate) : '—'}</b> CAPTCHA rate
          </span>
          <span>
            <b>{stats?.runs_last_7d ?? '—'}</b> runs (7d)
          </span>
        </div>
      </section>

      <section className="filters" aria-label="Lead filters">
        <input
          type="search"
          placeholder="Search brand, ASIN, title…"
          value={filters.q}
          onChange={(e) => {
            setFilters({ ...filters, q: e.target.value })
            setOffset(0)
          }}
          aria-label="Text search"
        />
        <select
          value={filters.persona}
          onChange={(e) => {
            setFilters({ ...filters, persona: e.target.value })
            setOffset(0)
          }}
          aria-label="Persona"
        >
          <option value="">Persona: all</option>
          {PERSONAS.filter(Boolean).map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <select
          value={filters.contact_quality}
          onChange={(e) => {
            setFilters({ ...filters, contact_quality: e.target.value })
            setOffset(0)
          }}
          aria-label="Contact quality"
        >
          <option value="">Contact: all</option>
          {CONTACT_QUALITIES.filter(Boolean).map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Country (e.g. US)"
          value={filters.country}
          onChange={(e) => {
            setFilters({ ...filters, country: e.target.value })
            setOffset(0)
          }}
          aria-label="Country"
        />
        <input
          type="number"
          min={0}
          placeholder="Max reviews"
          value={filters.max_reviews}
          onChange={(e) => {
            setFilters({ ...filters, max_reviews: e.target.value })
            setOffset(0)
          }}
          aria-label="Max reviews"
        />
        <select
          value={filters.status}
          onChange={(e) => {
            setFilters({ ...filters, status: e.target.value })
            setOffset(0)
          }}
          aria-label="Outreach status"
        >
          <option value="">Status: all</option>
          {STATUSES.filter(Boolean).map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          value={filters.sort}
          onChange={(e) => setFilters({ ...filters, sort: e.target.value })}
          aria-label="Sort"
        >
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <button
          className="btn btn-ghost"
          onClick={() => {
            setFilters(EMPTY_FILTERS)
            setOffset(0)
          }}
        >
          Clear
        </button>
      </section>

      <div className="table-toolbar">
        <span>
          {total} lead{total === 1 ? '' : 's'}
          {selectedCount > 0 && ` · ${selectedCount} selected`}
        </span>
        <span className="spacer" />
        <label>
          Format
          <select value={exportFormat} onChange={(e) => setExportFormat(e.target.value as 'csv' | 'xlsx')}>
            <option value="csv">CSV</option>
            <option value="xlsx">XLSX</option>
          </select>
        </label>
        <button className="btn btn-primary" onClick={exportFiltered} disabled={exporting}>
          {exporting ? 'Exporting…' : 'Export filtered leads'}
        </button>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {loading ? (
        <p>Loading leads…</p>
      ) : leads.length === 0 ? (
        <p className="empty">No leads match the current filters.</p>
      ) : (
        <div className="table-wrap">
          <table className="leads-table">
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleSelectAll}
                    aria-label="Select all rows on this page"
                  />
                </th>
                <th>Brand</th>
                <th>ASIN / Title</th>
                <th>Reviews</th>
                <th>Persona</th>
                <th>Contact</th>
                <th>Email</th>
                <th>Pitch hook</th>
                <th>Status</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {leads.map((lead) => (
                <tr key={lead.id} className={lead.needs_review ? 'needs-review' : ''}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(lead.id)}
                      onChange={() => toggleSelect(lead.id)}
                      aria-label={`Select ${lead.brand}`}
                    />
                  </td>
                  <td>
                    <strong>{lead.brand}</strong>
                    <br />
                    <span className="muted">{lead.seller_country ?? '—'}</span>
                    {lead.needs_review && <span className="flag">needs review</span>}
                  </td>
                  <td>
                    {lead.asin ? (
                      <a
                        href={`https://www.amazon.com/dp/${lead.asin}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {lead.asin}
                      </a>
                    ) : (
                      '—'
                    )}
                    <br />
                    <span className="muted">{lead.title ?? ''}</span>
                  </td>
                  <td>
                    {lead.reviews ?? '—'}
                    <br />
                    <span className="muted">{lead.rating ? `★ ${lead.rating}` : ''}</span>
                  </td>
                  <td>
                    <span className={`badge badge-${lead.persona.toLowerCase()}`}>{lead.persona}</span>
                  </td>
                  <td>
                    {lead.contact_quality}
                    <br />
                    <span className="muted">
                      {lead.founder_name ?? '—'}
                      {lead.founder_confidence != null ? ` (${pct(lead.founder_confidence)})` : ''}
                    </span>
                    <br />
                    {lead.linkedin_url ? (
                      <a href={lead.linkedin_url} target="_blank" rel="noreferrer">
                        LinkedIn
                      </a>
                    ) : null}
                    {lead.website ? (
                      <>
                        {' '}
                        <a href={lead.website} target="_blank" rel="noreferrer">
                          Site
                        </a>
                      </>
                    ) : null}
                  </td>
                  <td>
                    {lead.email ? (
                      <>
                        {lead.email}
                        <br />
                        <span className="muted">
                          conf. {pct(lead.email_confidence)}
                          {lead.email_is_personal ? ' · personal' : ' · generic'}
                        </span>
                        <br />
                        <button className="btn btn-small" onClick={() => copyEmail(lead)}>
                          {copiedId === lead.id ? 'Copied ✓' : 'Copy email'}
                        </button>
                      </>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td className="pitch">{lead.pitch_hook ?? '—'}</td>
                  <td>
                    <select
                      value={lead.status}
                      disabled={savingId === lead.id}
                      onChange={(e) => updateLead(lead.id, { status: e.target.value as LeadStatus })}
                      aria-label={`Status for ${lead.brand}`}
                    >
                      {(['NEW', 'SHORTLISTED', 'CONTACTED', 'SKIP'] as LeadStatus[]).map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      type="text"
                      placeholder="Add a note…"
                      value={notesDraft[lead.id] ?? ''}
                      onChange={(e) => setNotesDraft({ ...notesDraft, [lead.id]: e.target.value })}
                      onBlur={() => {
                        const note = notesDraft[lead.id]
                        if (note !== undefined && note.trim() !== '') {
                          updateLead(lead.id, { notes: note.trim() })
                          setNotesDraft((d) => {
                            const next = { ...d }
                            delete next[lead.id]
                            return next
                          })
                        }
                      }}
                      aria-label={`Notes for ${lead.brand}`}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="pagination">
        <button
          className="btn btn-ghost"
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          ← Previous
        </button>
        <span>
          Showing {leads.length === 0 ? 0 : offset + 1}–{offset + leads.length} of {total}
        </span>
        <button
          className="btn btn-ghost"
          disabled={offset + leads.length >= total}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next →
        </button>
      </div>
    </div>
  )
}
