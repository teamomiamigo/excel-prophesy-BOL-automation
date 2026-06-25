import { useState, useEffect } from 'react';

const TH = {
  padding: '7px 10px',
  background: '#374151',
  color: '#fff',
  textAlign: 'left',
  fontSize: 11,
  fontWeight: 600,
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
  whiteSpace: 'nowrap',
};

const TD = {
  padding: '7px 10px',
  borderBottom: '1px solid #f3f4f6',
  fontSize: 13,
  whiteSpace: 'nowrap',
};

const TD_R = { ...TD, textAlign: 'right' };

function fmtMoney(val) {
  if (val == null) return '—';
  return `$${parseFloat(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtCostPct(val) {
  if (val == null) return '—';
  return `${(parseFloat(val) * 100).toFixed(2)}%`;
}

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts).toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function StatusBadge({ status }) {
  const colors = {
    approved: { bg: '#dcfce7', color: '#166534' },
    flagged:  { bg: '#fef3c7', color: '#92400e' },
    pending:  { bg: '#f3f4f6', color: '#6b7280' },
  };
  const s = colors[status] || colors.pending;
  return (
    <span style={{
      background: s.bg,
      color: s.color,
      borderRadius: 3,
      padding: '2px 7px',
      fontSize: 11,
      fontWeight: 600,
      textTransform: 'uppercase',
    }}>
      {status}
    </span>
  );
}

export default function LogSection() {
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');

  async function fetchLogs(sd, ed) {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (sd) params.set('start_date', sd);
      if (ed) params.set('end_date', ed);
      const res = await fetch(`/api/logs?${params}`);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      setRecords(await res.json());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchLogs('', ''); }, []);

  function handleFilter(e) {
    e.preventDefault();
    fetchLogs(startDate, endDate);
  }

  function handleExport() {
    const params = new URLSearchParams();
    if (startDate) params.set('start_date', startDate);
    if (endDate) params.set('end_date', endDate);
    window.location.href = `/api/logs/export?${params}`;
  }

  return (
    <section>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <h2 style={{ fontSize: 14, fontWeight: 600, color: '#374151' }}>
          Log — Approved Records ({records.length})
        </h2>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <form onSubmit={handleFilter} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <input
              type="date"
              value={startDate}
              onChange={e => setStartDate(e.target.value)}
              style={{ border: '1px solid #d1d5db', borderRadius: 4, padding: '4px 8px', fontSize: 12 }}
            />
            <span style={{ color: '#6b7280', fontSize: 12 }}>to</span>
            <input
              type="date"
              value={endDate}
              onChange={e => setEndDate(e.target.value)}
              style={{ border: '1px solid #d1d5db', borderRadius: 4, padding: '4px 8px', fontSize: 12 }}
            />
            <button
              type="submit"
              style={{
                background: '#374151',
                color: '#fff',
                border: 'none',
                borderRadius: 4,
                padding: '5px 12px',
                fontSize: 12,
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              Filter
            </button>
            {(startDate || endDate) && (
              <button
                type="button"
                onClick={() => { setStartDate(''); setEndDate(''); fetchLogs('', ''); }}
                style={{
                  background: '#fff',
                  color: '#6b7280',
                  border: '1px solid #d1d5db',
                  borderRadius: 4,
                  padding: '5px 10px',
                  fontSize: 12,
                  cursor: 'pointer',
                }}
              >
                Clear
              </button>
            )}
          </form>
          <button
            onClick={handleExport}
            style={{
              background: '#fff',
              color: '#374151',
              border: '1px solid #d1d5db',
              borderRadius: 4,
              padding: '5px 12px',
              fontSize: 12,
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            Export CSV
          </button>
        </div>
      </div>

      {error && (
        <div style={{ color: '#991b1b', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 4, padding: '8px 12px', marginBottom: 12, fontSize: 13 }}>
          {error}
        </div>
      )}

      {loading ? (
        <div style={{ padding: 32, textAlign: 'center', color: '#9ca3af' }}>Loading log…</div>
      ) : records.length === 0 ? (
        <div style={{ padding: 24, textAlign: 'center', color: '#9ca3af', background: '#fff', borderRadius: 8, border: '1px solid #e5e7eb', fontSize: 13 }}>
          No records found for the selected date range.
        </div>
      ) : (
        <div style={{ overflowX: 'auto', borderRadius: 8, border: '1px solid #e5e7eb' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff' }}>
            <thead>
              <tr>
                <th style={TH}>Date</th>
                <th style={TH}>Trip</th>
                <th style={TH}>Manifest</th>
                <th style={TH}>BOL</th>
                <th style={TH}>Invoice #</th>
                <th style={{ ...TH, textAlign: 'right' }}>Amount</th>
                <th style={{ ...TH, textAlign: 'right' }}>Calc Cost</th>
                <th style={{ ...TH, textAlign: 'right' }}>Cost %</th>
                <th style={TH}>Status</th>
                <th style={TH}>Approved By</th>
                <th style={TH}>Approved At</th>
                <th style={TH}>Notes</th>
                <th style={TH}>Sent to Accounting</th>
              </tr>
            </thead>
            <tbody>
              {records.map(r => (
                <tr key={r.id} style={{ background: r.status === 'approved' ? '#f9fafb' : r.status === 'flagged' ? '#fffbeb' : '#fff' }}>
                  <td style={{ ...TD, color: '#6b7280', fontSize: 12 }}>
                    {fmtTime(r.created_at)}
                  </td>
                  <td style={{ ...TD, color: '#6b7280' }}>{r.technique_trip || '—'}</td>
                  <td style={TD}>{r.manifest || '—'}</td>
                  <td style={TD}>{r.bol_number ?? <span style={{ color: '#9ca3af' }}>—</span>}</td>
                  <td style={{ ...TD, fontWeight: 600 }}>{r.invoice_number || '—'}</td>
                  <td style={{ ...TD_R, fontWeight: 600 }}>{fmtMoney(r.amount)}</td>
                  <td style={TD_R}>{fmtMoney(r.access_prog)}</td>
                  <td style={TD_R}>{fmtCostPct(r.cost_pct)}</td>
                  <td style={TD}><StatusBadge status={r.status} /></td>
                  <td style={{ ...TD, color: '#6b7280' }}>{r.approved_by || '—'}</td>
                  <td style={{ ...TD, color: '#6b7280', fontSize: 12 }}>{fmtTime(r.approved_at)}</td>
                  <td style={{ ...TD, color: '#6b7280', fontSize: 12, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {r.notes || '—'}
                  </td>
                  <td style={{ ...TD, color: '#6b7280', fontSize: 12 }}>
                    {r.accounting_exported_at ? fmtTime(r.accounting_exported_at) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
