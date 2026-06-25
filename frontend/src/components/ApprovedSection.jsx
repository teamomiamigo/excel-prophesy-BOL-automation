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

function fmtNum(val) {
  if (val == null) return '—';
  return parseInt(val).toLocaleString('en-US');
}

function fmtCostPct(costPct) {
  if (costPct == null) return '—';
  return `${(costPct * 100).toFixed(2)}%`;
}

export default function ApprovedSection({
  approvedBols,
  loading,
  sendLoading,
  sendConfirmPending,
  sidLoading,
  unapprovingId,
  onUnapprove,
  onSendToAccounting,
  onConfirmSend,
  onCancelSend,
  onExportProphecy,
}) {
  const sidCount = approvedBols.filter(b => b.needs_sid_export).length;
  return (
    <section>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <h2 style={{ fontSize: 14, fontWeight: 600, color: '#374151' }}>
          Approved Today ({approvedBols.length})
        </h2>

        {approvedBols.length > 0 && !sendConfirmPending && (
          <div style={{ display: 'flex', gap: 8 }}>
            {sidCount > 0 && (
              <button
                onClick={onExportProphecy}
                disabled={sidLoading}
                title={`Download Prophecy SID import file for ${sidCount} Type-A record(s) that need a BOL created`}
                style={{
                  background: '#fff',
                  color: '#374151',
                  border: '1px solid #d1d5db',
                  borderRadius: 6,
                  padding: '8px 16px',
                  fontWeight: 600,
                  fontSize: 13,
                  opacity: sidLoading ? 0.6 : 1,
                  cursor: sidLoading ? 'not-allowed' : 'pointer',
                }}
              >
                {sidLoading ? 'Generating…' : `Export to Prophecy (${sidCount})`}
              </button>
            )}
            <button
              onClick={onSendToAccounting}
              disabled={sendLoading}
              style={{
                background: '#2D6A4F',
                color: '#fff',
                border: 'none',
                borderRadius: 6,
                padding: '8px 18px',
                fontWeight: 600,
                fontSize: 13,
                opacity: sendLoading ? 0.6 : 1,
                cursor: sendLoading ? 'not-allowed' : 'pointer',
              }}
            >
              {sendLoading ? 'Sending…' : `Send to Accounting (${approvedBols.length})`}
            </button>
          </div>
        )}

        {sendConfirmPending && (
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            background: '#fff',
            border: '1px solid #d1d5db',
            borderRadius: 6,
            padding: '8px 14px',
          }}>
            <span style={{ fontSize: 13, color: '#374151' }}>
              Send {approvedBols.length} approved record{approvedBols.length !== 1 ? 's' : ''} to Mary and Katie?
            </span>
            <button
              onClick={onConfirmSend}
              style={{
                background: '#2D6A4F',
                color: '#fff',
                border: 'none',
                borderRadius: 4,
                padding: '5px 14px',
                fontWeight: 600,
                fontSize: 13,
                cursor: 'pointer',
              }}
            >
              Confirm
            </button>
            <button
              onClick={onCancelSend}
              style={{
                background: '#fff',
                color: '#6b7280',
                border: '1px solid #d1d5db',
                borderRadius: 4,
                padding: '5px 14px',
                fontSize: 13,
                cursor: 'pointer',
              }}
            >
              Cancel
            </button>
          </div>
        )}
      </div>

      {loading ? (
        <div style={{ padding: 24, textAlign: 'center', color: '#9ca3af' }}>Loading…</div>
      ) : approvedBols.length === 0 ? (
        <div style={{
          padding: 20,
          textAlign: 'center',
          color: '#9ca3af',
          background: '#fff',
          borderRadius: 8,
          border: '1px solid #e5e7eb',
          fontSize: 13,
        }}>
          No approved records yet today.
        </div>
      ) : (
        <div style={{ overflowX: 'auto', borderRadius: 8, border: '1px solid #e5e7eb' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff' }}>
            <thead>
              <tr>
                <th style={TH}>Trip</th>
                <th style={TH}>Manifest</th>
                <th style={TH}>BOL</th>
                <th style={{ ...TH, textAlign: 'right' }}>Wgt</th>
                <th style={{ ...TH, textAlign: 'right' }}>Pallets</th>
                <th style={{ ...TH, textAlign: 'right' }}>PCS</th>
                <th style={TH}>Invoice #</th>
                <th style={{ ...TH, textAlign: 'right' }}>Calc Cost</th>
                <th style={{ ...TH, textAlign: 'right' }}>Amount</th>
                <th style={{ ...TH, textAlign: 'right' }}>Cost %</th>
                <th style={TH}>Notes</th>
                <th style={TH}>Approved By</th>
                <th style={TH}>Approved At</th>
                <th style={{ ...TH, textAlign: 'center' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {approvedBols.map(bol => {
                const isUnapproving = unapprovingId === bol.id;
                return (
                  <tr key={bol.id} style={{ background: '#f0fdf4' }}>
                    <td style={TD}>{bol.technique_trip || '—'}</td>
                    <td style={TD}>{bol.manifest || '—'}</td>
                    <td style={TD}>{bol.bol_number ?? <span style={{ color: '#9ca3af' }}>—</span>}</td>
                    <td style={TD_R}>{fmtNum(bol.technique_weight)}</td>
                    <td style={TD_R}>{fmtNum(bol.technique_pallets)}</td>
                    <td style={TD_R}>{fmtNum(bol.technique_pcs)}</td>
                    <td style={{ ...TD, fontWeight: 600 }}>{bol.invoice_number || '—'}</td>
                    <td style={TD_R}>{fmtMoney(bol.access_prog)}</td>
                    <td style={{ ...TD_R, fontWeight: 600 }}>{fmtMoney(bol.amount)}</td>
                    <td style={{ ...TD_R, color: '#16a34a', fontWeight: 600 }}>
                      {fmtCostPct(bol.cost_pct)}
                    </td>
                    <td style={{ ...TD, color: '#6b7280', fontSize: 12, maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {bol.notes || '—'}
                    </td>
                    <td style={{ ...TD, color: '#6b7280' }}>{bol.approved_by || 'coordinator'}</td>
                    <td style={{ ...TD, color: '#6b7280', fontSize: 12 }}>
                      {bol.approved_at
                        ? new Date(bol.approved_at).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
                        : '—'}
                    </td>
                    <td style={{ ...TD, textAlign: 'center' }}>
                      <button
                        onClick={() => onUnapprove(bol.id)}
                        disabled={isUnapproving}
                        title="Move back to pending review"
                        style={{
                          background: isUnapproving ? '#f3f4f6' : '#fff',
                          color: isUnapproving ? '#9ca3af' : '#6b7280',
                          border: '1px solid #d1d5db',
                          borderRadius: 4,
                          padding: '4px 10px',
                          fontSize: 12,
                          fontWeight: 600,
                          cursor: isUnapproving ? 'not-allowed' : 'pointer',
                        }}
                      >
                        {isUnapproving ? '…' : '↩ Revert'}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
