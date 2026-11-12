import { useCallback, useEffect, useState } from 'react'
import { ApiError, apiFetch } from '../api'
import type { Run } from '../types'

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

export default function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<'dry' | 'live'>('dry')
  const [starting, setStarting] = useState(false)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [detail, setDetail] = useState<Run | null>(null)

  const loadRuns = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await apiFetch<Run[]>('/api/runs')
      setRuns(Array.isArray(data) ? data : [])
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load runs.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadRuns()
  }, [loadRuns])

  async function startRun() {
    setStarting(true)
    setError('')
    try {
      const data = await apiFetch<{ run_id: number }>('/api/runs', {
        method: 'POST',
        body: JSON.stringify({ mode }),
      })
      await loadRuns()
      setError('')
      setExpanded(data.run_id)
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.message
          : 'Failed to start run.',
      )
    } finally {
      setStarting(false)
    }
  }

  async function toggleDetail(run: Run) {
    if (expanded === run.id) {
      setExpanded(null)
      setDetail(null)
      return
    }
    setExpanded(run.id)
    try {
      setDetail(await apiFetch<Run>(`/api/runs/${run.id}`))
    } catch {
      setDetail(run)
    }
  }

  return (
    <div>
      <h1>Run history</h1>
      <p className="muted">
        Every hunt run is recorded with its counts. A killed run resumes its pending jobs on the
        next run — nothing is lost.
      </p>

      <div className="run-start">
        <label>
          Mode
          <select value={mode} onChange={(e) => setMode(e.target.value as 'dry' | 'live')}>
            <option value="dry">Dry run (fixtures, no live Amazon calls)</option>
            <option value="live">Live (requires PLH_LIVE=1 on the server)</option>
          </select>
        </label>
        <button className="btn btn-primary" onClick={startRun} disabled={starting}>
          {starting ? 'Starting…' : 'Start run'}
        </button>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {loading ? (
        <p>Loading runs…</p>
      ) : runs.length === 0 ? (
        <p className="empty">No runs yet.</p>
      ) : (
        <table className="leads-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Started</th>
              <th>Finished</th>
              <th>Status</th>
              <th>Brands seen</th>
              <th>New leads</th>
              <th>CAPTCHA blocks</th>
              <th>Email</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>#{run.id}</td>
                <td>{fmtTime(run.started_at)}</td>
                <td>{fmtTime(run.finished_at)}</td>
                <td>
                  <span className={`badge badge-${run.status}`}>{run.status}</span>
                </td>
                <td>{run.brands_seen ?? '—'}</td>
                <td>{run.leads_new ?? '—'}</td>
                <td>{run.captcha_blocks ?? '—'}</td>
                <td>{run.email_status ?? '—'}</td>
                <td>
                  <button className="btn btn-small" onClick={() => toggleDetail(run)}>
                    {expanded === run.id ? 'Hide' : 'Details'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {expanded !== null && detail && (
        <article className="result-card">
          <h2>Run #{detail.id} — {detail.status}</h2>
          <dl className="result-grid">
            <div>
              <dt>Keywords used</dt>
              <dd>{detail.keywords_used?.join(', ') ?? '—'}</dd>
            </div>
            <div>
              <dt>Leads total</dt>
              <dd>{detail.leads_total ?? '—'}</dd>
            </div>
            <div>
              <dt>Errors</dt>
              <dd>{detail.errors?.length ? detail.errors.join('; ') : 'None'}</dd>
            </div>
            <div>
              <dt>Summary</dt>
              <dd>{detail.summary_json ? JSON.stringify(detail.summary_json) : '—'}</dd>
            </div>
          </dl>
        </article>
      )}
    </div>
  )
}
