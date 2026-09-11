import { useEffect, useMemo, useState } from 'react'
import {
  PieChart, Pie, Cell, Tooltip, ResponsiveContainer,
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
} from 'recharts'

const REFRESH_MS = 30_000 // re-poll every 30s
const API_BASE = import.meta.env.VITE_API_URL // set in Vercel; empty locally
const SEVERITY_COLORS = {
  Critical: '#e34948',
  High: '#eb6834',
  Medium: '#eda100',
  Low: '#2a78d6',
  Informational: '#6b7280',
}

function todayKey() {
  // YYYY-MM-DD in the browser's local timezone
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function severityBadgeClass(name) {
  const n = (name || '').toLowerCase()
  if (n === 'critical') return 'badge badge-critical'
  if (n === 'high') return 'badge badge-high'
  if (n === 'medium') return 'badge badge-medium'
  if (n === 'informational') return 'badge badge-informational'
  return 'badge badge-low'
}

// Backend now sends a ready-made statusLabel (Created / Assigned /
// Acknowledged / In Progress / Pending Client / Pending SOC / Blocked /
// On Hold / Resolved / Closed - ...) based on the numeric incidentStatus
// code -- no more guessing from custom-field text.
function statusLabel(incident) {
  return incident.statusLabel || (incident.isClosed ? 'Closed' : 'Open')
}

function statusBadgeClass(incident) {
  const label = statusLabel(incident)
  if (label.startsWith('Closed') || label === 'Resolved') return 'badge badge-closed'
  if (label === 'In Progress') return 'badge badge-progress'
  if (label === 'Pending Client' || label === 'Pending SOC') return 'badge badge-pending'
  if (label === 'On Hold' || label === 'Blocked') return 'badge badge-hold'
  return 'badge badge-open'
}

export default function App() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [pending, setPending] = useState(null)

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const url = API_BASE
          ? `${API_BASE}/api/incidents/latest?t=${Date.now()}`
          : `/data/incidents_latest.json?t=${Date.now()}`
        const res = await fetch(url)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = await res.json()
        if (!cancelled) {
          setPayload(json)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(e.message)
      }
    }

    load()
    const interval = setInterval(load, REFRESH_MS)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  // Separate feed: Pending with Client / Pending with SOC / On Hold totals
  // and per-assignee breakdown, computed backend-side from the pending
  // window (from PENDING_SINCE_DATE, effectively "from the beginning") so
  // older still-open tickets don't disappear once the day rolls over.
  // Non-fatal if it fails -- the rest of the dashboard still works.
  useEffect(() => {
    let cancelled = false

    async function loadPending() {
      try {
        const url = API_BASE
          ? `${API_BASE}/api/incidents/pending?t=${Date.now()}`
          : `/data/pending_latest.json?t=${Date.now()}`
        const res = await fetch(url)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = await res.json()
        if (!cancelled) setPending(json)
      } catch (e) {
        console.warn('Could not load pending summary:', e.message)
      }
    }

    loadPending()
    const interval = setInterval(loadPending, REFRESH_MS)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  // Separate feed: every currently-open incident regardless of creation
  // date (In Progress, Pending Client, Pending SOC, On Hold, Blocked,
  // etc.), computed backend-side from the same wide pending window. This
  // is what the "Open incidents" table below uses -- /api/incidents/latest
  // alone only covers today's incidents, so an older still-open ticket
  // (e.g. started In Progress yesterday) would otherwise never show up.
  const [openData, setOpenData] = useState(null)

  useEffect(() => {
    let cancelled = false

    async function loadOpen() {
      try {
        const url = API_BASE
          ? `${API_BASE}/api/incidents/open?t=${Date.now()}`
          : `/data/open_latest.json?t=${Date.now()}`
        const res = await fetch(url)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = await res.json()
        if (!cancelled) setOpenData(json)
      } catch (e) {
        console.warn('Could not load open-incidents feed:', e.message)
      }
    }

    loadOpen()
    const interval = setInterval(loadOpen, REFRESH_MS)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  const incidents = payload?.incidents ?? []
  const isStale = payload && payload.date !== todayKey()

  const kpis = useMemo(() => {
    const total = incidents.length
    const closed = incidents.filter((i) => i.isClosed).length
    const open = total - closed
    const slaBreached = incidents.filter((i) => i.slaBreached).length
    return { total, open, closed, slaBreached }
  }, [incidents])

  const severityData = useMemo(() => {
    const counts = { Critical: 0, High: 0, Medium: 0, Low: 0, Informational: 0 }
    incidents.forEach((i) => {
      const name = i.severityName in counts ? i.severityName : 'Low'
      counts[name] += 1
    })
    return Object.entries(counts)
      .map(([name, value]) => ({ name, value }))
      .filter((s) => s.value > 0)
  }, [incidents])

  const hourlyData = useMemo(() => {
    const buckets = Array.from({ length: 24 }, (_, h) => ({ hour: `${h}:00`, count: 0 }))
    incidents.forEach((i) => {
      const ts = i.detectionDateTime || i.createdAt
      if (!ts) return
      const hour = new Date(ts).getHours()
      if (buckets[hour]) buckets[hour].count += 1
    })
    return buckets
  }, [incidents])

  // Backend already filters to non-closed and sorts by severity -- this
  // covers every open status (In Progress, Pending Client, Pending SOC,
  // On Hold, Blocked, etc.) no matter when the incident was created.
  const openIncidents = openData?.incidents ?? []

  const closedByClient = useMemo(() => {
    const map = {}
    incidents.forEach((i) => {
      const client = i.clientName || 'Unknown'
      if (!map[client]) map[client] = { client, open: 0, closed: 0 }
      if (i.isClosed) map[client].closed += 1
      else map[client].open += 1
    })
    return Object.values(map).sort((a, b) => (b.open + b.closed) - (a.open + a.closed))
  }, [incidents])

  const resolverLeaderboard = useMemo(() => {
    const counts = {}
    incidents.filter((i) => i.isClosed).forEach((i) => {
      const name = i.assigneeName || 'Unassigned'
      counts[name] = (counts[name] || 0) + 1
    })
    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 6)
    const max = sorted.length ? sorted[0][1] : 1
    return sorted.map(([name, count]) => ({ name, count, pct: Math.round((count / max) * 100) }))
  }, [incidents])

  const pendingTotals = pending?.totals ?? {}
  const pendingByAssignee = pending?.byAssignee ?? []

  return (
    <div className="dashboard">
      <div className="dash-header">
        <h1>SOC incident dashboard</h1>
        <span className="meta">
          {payload ? `Showing ${payload.date} · updated ${new Date(payload.generatedAt).toLocaleTimeString()}` : 'Loading…'}
        </span>
      </div>

      {(error || (isStale && !error)) && (
        <div className="status-banner stale">
          {error
            ? `Couldn't load incident data (${error}). Make sure the backend is running and reachable.`
            : `Data shown is from ${payload.date}. Waiting for today's first refresh.`}
        </div>
      )}

      {/* Row: KPI cards */}
      <div className="kpi-row">
        <div className="kpi-card">
          <p className="label">Total incidents today</p>
          <p className="value">{kpis.total}</p>
        </div>
        <div className="kpi-card">
          <p className="label">Open</p>
          <p className="value" style={{ color: 'var(--danger)' }}>{kpis.open}</p>
        </div>
        <div className="kpi-card">
          <p className="label">Closed</p>
          <p className="value" style={{ color: 'var(--success)' }}>{kpis.closed}</p>
        </div>
        <div className="kpi-card">
          <p className="label">SLA breached</p>
          <p className="value" style={{ color: 'var(--warning)' }}>{kpis.slaBreached}</p>
        </div>
      </div>

      {/* Row: Pending detections by assignee | Incidents by hour of day */}
      <div className="row-2col">
        <div className="panel">
          <h2>
            Pending detections by assignee
            {pending?.sinceDate ? ` (since ${pending.sinceDate})` : pending?.lookbackDays ? ` (last ${pending.lookbackDays} days)` : ''}
          </h2>
          <div className="panel-body">
            {pendingByAssignee.length === 0 ? (
              <p className="empty-state">Nothing pending right now.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Assignee</th>
                    <th>Client</th>
                    <th>SOC</th>
                    <th>Hold</th>
                  </tr>
                </thead>
                <tbody>
                  {pendingByAssignee.map((r) => (
                    <tr key={r.name}>
                      <td>{r.name}</td>
                      <td>{r.pendingWithClient}</td>
                      <td>{r.pendingWithSoc}</td>
                      <td>{r.onHold}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="panel">
          <h2>Incidents by hour of day (peak time)</h2>
          <div className="panel-body chart-body">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={hourlyData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a2f3b" vertical={false} />
                <XAxis dataKey="hour" tick={{ fontSize: 9, fill: '#9aa1b1' }} interval={2} axisLine={false} tickLine={false} />
                <YAxis tick={{ fontSize: 9, fill: '#9aa1b1' }} axisLine={false} tickLine={false} width={24} />
                <Tooltip contentStyle={{ background: '#1d212c', border: '1px solid #2a2f3b', fontSize: 12 }} />
                <Bar dataKey="count" fill="#2a78d6" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      {/* Row: By severity (donut) | Pending with client | Pending with SOC | On hold */}
      <div className="row-severity">
        <div className="panel">
          <h2>By severity</h2>
          <div className="panel-body severity-body">
            <div className="chart-body">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={severityData} dataKey="value" nameKey="name" innerRadius="55%" outerRadius="85%" paddingAngle={2}>
                    {severityData.map((entry) => (
                      <Cell key={entry.name} fill={SEVERITY_COLORS[entry.name]} />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={{ background: '#1d212c', border: '1px solid #2a2f3b', fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <div className="severity-legend">
              {severityData.map((s) => (
                <span key={s.name}>
                  <span className="dot" style={{ background: SEVERITY_COLORS[s.name] }} />
                  {s.name} {s.value}
                </span>
              ))}
            </div>
          </div>
        </div>

        <div className="kpi-card pending-card">
          <p className="label">Pending with client</p>
          <p className="value" style={{ color: 'var(--warning)' }}>{pendingTotals.pendingWithClient ?? '—'}</p>
        </div>
        <div className="kpi-card pending-card">
          <p className="label">Pending with SOC</p>
          <p className="value" style={{ color: 'var(--danger)' }}>{pendingTotals.pendingWithSoc ?? '—'}</p>
        </div>
        <div className="kpi-card pending-card">
          <p className="label">On hold</p>
          <p className="value" style={{ color: 'var(--text-secondary)' }}>{pendingTotals.onHold ?? '—'}</p>
        </div>
      </div>

      {/* Row: Open incidents (full width, scrolls internally) */}
      <div className="panel row-open">
        <h2>Open incidents ({openIncidents.length})</h2>
        <div className="panel-body">
          {openIncidents.length === 0 ? (
            <p className="empty-state">Nothing open right now.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Client</th>
                  <th>Severity</th>
                  <th>Status</th>
                  <th>Assignee</th>
                  <th>SLA</th>
                  <th>Title</th>
                </tr>
              </thead>
              <tbody>
                {openIncidents.map((i) => (
                  <tr key={i.id}>
                    <td>{i.clientName}</td>
                    <td><span className={severityBadgeClass(i.severityName)}>{i.severityName}</span></td>
                    <td><span className={statusBadgeClass(i)}>{statusLabel(i)}</span></td>
                    <td>{i.assigneeName || '—'}</td>
                    <td className={i.slaBreached ? 'badge-breached' : 'badge-ontrack'}>
                      {i.slaBreached ? 'Breached' : 'On track'}
                    </td>
                    <td>{i.title}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* Row: Closed vs open by client | Top resolvers today */}
      <div className="row-2col">
        <div className="panel">
          <h2>Closed vs open by client</h2>
          <div className="panel-body">
            <table>
              <thead>
                <tr>
                  <th>Client</th>
                  <th>Open</th>
                  <th>Closed</th>
                </tr>
              </thead>
              <tbody>
                {closedByClient.map((c) => (
                  <tr key={c.client}>
                    <td>{c.client}</td>
                    <td style={{ color: c.open > 0 ? 'var(--danger)' : 'var(--text-secondary)' }}>{c.open}</td>
                    <td style={{ color: 'var(--success)' }}>{c.closed}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel">
          <h2>Top resolvers today</h2>
          <div className="panel-body">
            {resolverLeaderboard.length === 0 ? (
              <p className="empty-state">No closed incidents yet today.</p>
            ) : (
              resolverLeaderboard.map((r) => (
                <div className="leaderboard-row" key={r.name}>
                  <span className="name">{r.name}</span>
                  <div className="track"><div className="fill" style={{ width: `${r.pct}%` }} /></div>
                  <span className="count">{r.count}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
