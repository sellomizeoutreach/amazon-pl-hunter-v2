import { useState } from 'react'
import { Link, NavLink, Route, Routes } from 'react-router-dom'
import { clearToken, getToken, setToken } from './api'
import AuditPage from './pages/AuditPage'
import LeadsPage from './pages/LeadsPage'
import RunsPage from './pages/RunsPage'

function TokenGate({ children }: { children: React.ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => getToken())
  const [input, setInput] = useState('')
  const [error, setError] = useState('')

  if (token) {
    return (
      <>
        <div className="token-bar">
          <span>API token saved in this browser.</span>
          <button
            className="btn btn-ghost"
            onClick={() => {
              clearToken()
              setTokenState(null)
              setInput('')
            }}
          >
            Change token
          </button>
        </div>
        {children}
      </>
    )
  }

  return (
    <div className="token-gate">
      <h2>Enter your API token</h2>
      <p>
        The PL Hunter API requires a token (<code>X-API-Token</code>). Enter it once — it is
        stored only in this browser&apos;s localStorage.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (!input.trim()) {
            setError('Enter a token first.')
            return
          }
          setToken(input.trim())
          setTokenState(input.trim())
        }}
      >
        <input
          type="password"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="PLH_API_TOKEN"
          autoComplete="off"
          aria-label="API token"
        />
        <button className="btn btn-primary" type="submit">
          Save token
        </button>
      </form>
      {error && <p className="error">{error}</p>}
    </div>
  )
}

export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">
          Amazon PL Hunter
        </Link>
        <nav>
          <NavLink to="/" end>
            Leads
          </NavLink>
          <NavLink to="/audit">Brand Audit</NavLink>
          <NavLink to="/runs">Runs</NavLink>
        </nav>
      </header>
      <main>
        <TokenGate>
          <Routes>
            <Route path="/" element={<LeadsPage />} />
            <Route path="/audit" element={<AuditPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="*" element={<p>Page not found.</p>} />
          </Routes>
        </TokenGate>
      </main>
    </div>
  )
}
