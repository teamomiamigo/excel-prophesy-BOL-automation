// AI agent proposals — a flat table, not grouped like ApprovedSection.jsx,
// since proposals aren't naturally grouped by sender. Accept/Reject route
// through the same per-record mutation the manual Approve/Flag buttons use
// (see backend/main.py::_accept_proposal) — this component never mutates
// anything itself, only calls the handlers App.jsx passes down.

const ACTION_STYLE = {
  approve: { label: 'Approve', color: '#2D6A4F', bg: '#f0fdf4', border: '#bbf7d0' },
  needs_review: { label: 'Needs Review', color: '#92400e', bg: '#fef3c7', border: '#fcd34d' },
  flag: { label: 'Flag', color: '#dc2626', bg: '#fef2f2', border: '#fecaca' },
};

const TH = { padding: '8px 10px', fontSize: 11, fontWeight: 700, color: '#6b7280', textTransform: 'uppercase', letterSpacing: '0.03em' };
const TD = { padding: '8px 10px' };

function fmtMoney(val) {
  if (val == null) return '—';
  return `$${parseFloat(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtCostPct(val) {
  if (val == null) return 'N/A';
  return `${(val * 100).toFixed(2)}%`;
}

export default function AgentActivitySection({
  proposals,
  loading,
  acceptingId,
  rejectingId,
  onAccept,
  onReject,
  onRunAgent,
  runAgentLoading,
}) {
  const pendingCount = proposals.filter(p => p.status === 'pending').length;

  return (
    <div style={{ background: '#fff', border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden' }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '12px 16px', borderBottom: '1px solid #e5e7eb', background: '#f9fafb',
      }}>
        <div>
          <span style={{ fontWeight: 700, fontSize: 14, color: '#374151' }}>
            <span style={{ color: '#6366f1', marginRight: 6 }}>🤖</span>
            AI Agent Proposals
          </span>
          <span style={{ marginLeft: 10, fontSize: 12, color: '#6b7280' }}>
            {pendingCount} pending review
          </span>
        </div>
        <button
          onClick={onRunAgent}
          disabled={runAgentLoading}
          title="Pull new invoices, classify every pending record, and email Katie a summary"
          style={{
            background: runAgentLoading ? '#e5e7eb' : '#6366f1',
            color: runAgentLoading ? '#9ca3af' : '#fff',
            border: 'none',
            borderRadius: 5,
            padding: '6px 14px',
            fontWeight: 600,
            fontSize: 12,
            cursor: runAgentLoading ? 'not-allowed' : 'pointer',
          }}
        >
          {runAgentLoading ? 'Running…' : '🤖 Run AI Agent'}
        </button>
      </div>

      {loading ? (
        <div style={{ padding: 24, textAlign: 'center', color: '#9ca3af', fontSize: 13 }}>Loading…</div>
      ) : proposals.length === 0 ? (
        <div style={{ padding: 32, textAlign: 'center', color: '#9ca3af', fontSize: 13 }}>
          No AI proposals yet — click "Run AI Agent" above.
        </div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr>
              <th style={TH}>Invoice / Trip</th>
              <th style={TH}>Amount</th>
              <th style={TH}>Cost %</th>
              <th style={TH}>Recommendation</th>
              <th style={TH}>Confidence</th>
              <th style={{ ...TH, minWidth: 260 }}>Reasoning</th>
              <th style={{ ...TH, textAlign: 'center' }}>Status</th>
              <th style={{ ...TH, textAlign: 'center' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {proposals.map(p => {
              const style = ACTION_STYLE[p.recommended_action] || ACTION_STYLE.needs_review;
              const isPending = p.status === 'pending';
              const isAccepting = acceptingId === p.id;
              const isRejecting = rejectingId === p.id;
              const busy = isAccepting || isRejecting;
              return (
                <tr key={p.id} style={{ borderBottom: '1px solid #f3f4f6' }}>
                  <td style={TD}>{p.invoice_number || p.technique_trip || '—'}</td>
                  <td style={TD}>{fmtMoney(p.amount)}</td>
                  <td style={TD}>{fmtCostPct(p.cost_pct)}</td>
                  <td style={TD}>
                    <span style={{
                      background: style.bg, color: style.color, border: `1px solid ${style.border}`,
                      borderRadius: 4, padding: '2px 8px', fontWeight: 700, fontSize: 11,
                    }}>
                      {style.label}
                    </span>
                  </td>
                  <td style={TD}>{Math.round(p.confidence * 100)}%</td>
                  <td style={{ ...TD, color: '#374151', fontSize: 12 }}>
                    <span style={{ fontSize: 9, marginRight: 4, opacity: 0.85, color: '#6366f1', fontWeight: 700 }}>AI</span>
                    {p.reasoning}
                  </td>
                  <td style={{
                    ...TD, textAlign: 'center', textTransform: 'capitalize',
                    color: isPending ? '#6b7280' : (p.status === 'accepted' ? '#2D6A4F' : '#9ca3af'),
                  }}>
                    {p.status}
                  </td>
                  <td style={{ ...TD, textAlign: 'center' }}>
                    {isPending ? (
                      <div style={{ display: 'flex', gap: 6, justifyContent: 'center' }}>
                        <button
                          onClick={() => onAccept(p.id)}
                          disabled={busy}
                          style={{
                            background: isAccepting ? '#d1fae5' : '#2D6A4F',
                            color: isAccepting ? '#065f46' : '#fff',
                            border: 'none', borderRadius: 4, padding: '4px 10px',
                            fontSize: 12, fontWeight: 600,
                            cursor: busy ? 'not-allowed' : 'pointer',
                          }}
                        >
                          {isAccepting ? '…' : '✓ Accept'}
                        </button>
                        <button
                          onClick={() => onReject(p.id)}
                          disabled={busy}
                          style={{
                            background: '#fff', color: '#6b7280', border: '1px solid #d1d5db',
                            borderRadius: 4, padding: '4px 10px', fontSize: 12, fontWeight: 600,
                            cursor: busy ? 'not-allowed' : 'pointer',
                          }}
                        >
                          {isRejecting ? '…' : 'Reject'}
                        </button>
                      </div>
                    ) : (
                      <span style={{ fontSize: 11, color: '#9ca3af' }}>—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
