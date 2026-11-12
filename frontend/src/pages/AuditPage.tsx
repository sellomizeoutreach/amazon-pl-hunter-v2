import { useState } from 'react'
import { ApiError, apiFetch } from '../api'
import type { AuditResult } from '../types'

function pct(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return `${Math.round(n * 100)}%`
}

export default function AuditPage() {
  const [brand, setBrand] = useState('')
  const [asin, setAsin] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<AuditResult | null>(null)

  async function runAudit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setResult(null)
    if (!brand.trim() && !asin.trim()) {
      setError('Enter a brand name or an ASIN.')
      return
    }
    setLoading(true)
    try {
      const data = await apiFetch<AuditResult>('/api/audit', {
        method: 'POST',
        body: JSON.stringify({
          ...(brand.trim() ? { brand: brand.trim() } : {}),
          ...(asin.trim() ? { asin: asin.trim().toUpperCase() } : {}),
        }),
      })
      setResult(data)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Audit failed.')
    } finally {
      setLoading(false)
    }
  }

  const contacts = result?.contacts ?? {}

  return (
    <div>
      <h1>Brand Audit</h1>
      <p className="muted">
        On-demand audit for one brand or ASIN. This is separate from the automatic daily hunt —
        it never blocks or changes the lead queue.
      </p>

      <form className="audit-form" onSubmit={runAudit}>
        <label>
          Brand name
          <input
            type="text"
            value={brand}
            onChange={(e) => setBrand(e.target.value)}
            placeholder="e.g. Acme Outdoors"
          />
        </label>
        <label>
          ASIN
          <input
            type="text"
            value={asin}
            onChange={(e) => setAsin(e.target.value)}
            placeholder="e.g. B0XXXXXXXX"
            maxLength={10}
          />
        </label>
        <button className="btn btn-primary" type="submit" disabled={loading}>
          {loading ? 'Auditing…' : 'Run audit'}
        </button>
      </form>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {result && (
        <article className="result-card">
          <header>
            <h2>{result.brand ?? result.asin ?? 'Audit result'}</h2>
            {result.needs_review && <span className="flag">needs review</span>}
          </header>

          {result.catalog.incomplete && (
            <p className="warning" role="alert">
              Catalogue check incomplete
              {result.catalog.fail_reason ? `: ${result.catalog.fail_reason}` : ''} — the audit
              failed closed: persona is UNKNOWN and this result must be reviewed, not trusted.
            </p>
          )}

          <dl className="result-grid">
            <div>
              <dt>PL score</dt>
              <dd>
                {result.pl_score != null ? pct(result.pl_score) : '—'}
                {result.pl_reason ? ` — ${result.pl_reason}` : ''}
              </dd>
            </div>
            <div>
              <dt>Seller</dt>
              <dd>
                {result.seller_id ?? '—'} · {result.seller_country ?? 'Unknown'}
                {result.country_confidence != null
                  ? ` (${pct(result.country_confidence)} confidence)`
                  : ''}
              </dd>
            </div>
            <div>
              <dt>Persona</dt>
              <dd>
                <span className={`badge badge-${result.persona.toLowerCase()}`}>{result.persona}</span>
              </dd>
            </div>
            <div>
              <dt>Catalogue health</dt>
              <dd>
                {result.catalog.total_products ?? '—'} products · max reviews{' '}
                {result.catalog.max_reviews ?? '—'} · pages checked {result.catalog.pages_checked ?? '—'}
                <br />
                {result.catalog.all_under_100
                  ? 'All products under 100 reviews ✓'
                  : 'Not all products under 100 reviews'}
                {result.catalog.incomplete ? ' · incomplete' : ''}
              </dd>
            </div>
            <div>
              <dt>Pitch hook</dt>
              <dd>{result.pitch_hook ?? '—'}</dd>
            </div>
          </dl>

          <h3>Listing problems</h3>
          {result.listing_problems.length === 0 ? (
            <p className="muted">None found.</p>
          ) : (
            <ul>
              {result.listing_problems.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ul>
          )}

          <h3>Contacts</h3>
          {Object.keys(contacts).length === 0 ? (
            <p className="muted">No contacts found.</p>
          ) : (
            <dl className="result-grid">
              {Object.entries(contacts).map(([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>{typeof value === 'string' ? value : JSON.stringify(value)}</dd>
                </div>
              ))}
            </dl>
          )}
          <p className="muted">
            Confidence is per field; anything below 0.8 or flagged needs_review should be verified
            by hand before outreach.
          </p>
        </article>
      )}
    </div>
  )
}
