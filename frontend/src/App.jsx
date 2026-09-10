import { useEffect, useMemo, useState } from 'react'
import {
  PieChart, Pie, Cell, Tooltip, ResponsiveContainer,
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
} from 'recharts'

const REFRESH_MS = 30_000 // re-poll every 30s
const API_BASE = import.meta.env.VITE_API_URL // set in Vercel; empty locally
const SEVERITY_COLORS = { Critical: '#e34948', High: '#eb6834', Medium: '#eda100', Low: '#2a78d6' }

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
  return 'badge badge-low'
}

function statusLabel(incident) {
  if (incident.isClosed) return 'Closed'
  const cf = (incident.cf_incident_status || '').toLowerCase()
  if (cf.includes('progress')) return 'In progress'
  return 'Open'
}

function statusBadgeClass(incident) {
  const label = statusLabel(incident)
  if (label === 'Closed') return 'badge badge-closed'
  if (label === 'In progress') return 'badge badge-progress'
  return 'badge badge-open'
}

export default function App() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)

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
    const counts = { Critical: 0, High: 0, Medium: 0, Low: 0 }
    incidents.forEach((i) => {
      const name = i.severityName in counts ? i.severityName : 'Low'
      counts[name] += 1
    })
    return Object.entries(counts).map(([name, value]) => ({ name, value }))
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

  const openIncidents = useMemo(
    () => incidents.filter((i) => !i.isClosed)
      .sort((a, b) => (a.severityId ?? 9) - (b.severityId ?? 9)),
    [incidents]
  )

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

  return (
    <div className="dashboard">
      <div className="header">
        <h1>SOC incident dashboard</h1>
        <span className="meta">
          {payload ? `Showing ${payload.date} · updated ${new Date(payload.generatedAt).toLocaleTimeString()}` : 'Loading…'}
        </span>
      </div>

      {error && (
        <div className="status-banner stale">
          Couldn't load incident data ({error}). Make sure fetch_incidents.py has run and
          frontend/public/data/incidents_latest.json exists.
        </div>
      )}

      {isStale && !error && (
        <div className="status-banner stale">
          Data shown is from {payload.date}. Waiting for today's first refresh from fetch_incidents.py.
        </div>
      )}

      <div className="kpi-grid">
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

      <div className="panel-grid">
        <div className="panel">
          <h2>By severity</h2>
          <ResponsiveContainer width="100%" height={220}>
            <PieChart>
              <Pie data={severityData} dataKey="value" nameKey="name" innerRadius={55} outerRadius={85} paddingAngle={2}>
                {severityData.map((entry) => (
                  <Cell key={entry.name} fill={SEVERITY_COLORS[entry.name]} />
                ))}
              </Pie>
              <Tooltip contentStyle={{ background: '#1d212c', border: '1px solid #2a2f3b', fontSize: 13 }} />
            </PieChart>
          </ResponsiveContainer>
          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12, color: 'var(--text-secondary)', marginTop: 8 }}>
            {severityData.map((s) => (
              <span key={s.name} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: SEVERITY_COLORS[s.name] }} />
                {s.name} {s.value}
              </span>
            ))}
          </div>
        </div>

        <div className="panel">
          <h2>Incidents by hour of day (peak time)</h2>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={hourlyData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2a2f3b" vertical={false} />
              <XAxis dataKey="hour" tick={{ fontSize: 10, fill: '#9aa1b1' }} interval={2} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 10, fill: '#9aa1b1' }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={{ background: '#1d212c', border: '1px solid #2a2f3b', fontSize: 13 }} />
              <Bar dataKey="count" fill="#2a78d6" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="panel" style={{ marginBottom: 24 }}>
        <h2>Open incidents ({openIncidents.length})</h2>
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

      <div className="panel-grid">
        <div className="panel">
          <h2>Closed vs open by client</h2>
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

        <div className="panel">
          <h2>Top resolvers today</h2>
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
  )
}
