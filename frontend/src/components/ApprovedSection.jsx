import { useState } from 'react';

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
  sidLoading,
  sidExportedThisSession,
  unapprovingId,
  onUnapprove,
  onConfirmSend,
  onExportProphecy,
}) {
  const [showModal, setShowModal] = useState(false);

  const sidCount  = approvedBols.filter(b => b.needs_sid_export).length;
  const bCount    = approvedBols.length - sidCount;
  const canSend   = sidCount === 0 || sidExportedThisSession;

  return (
    <section>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <h2 style={{ fontSize: 14, fontWeight: 600, color: '#374151' }}>
          Approved Today ({approvedBols.length})
        </h2>

        {approvedBols.length > 0 && (
          <button
            onClick={() => setShowModal(true)}
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
            {sendLoading ? 'Sending…' : 'Finalize & Export'}
          </button>
        )}
      </div>

      {/* Finalize & Export modal */}
      {showModal && (
        <div
          onClick={() => setShowModal(false)}
          style={{
            position: 'fixed', inset: 0,
            background: 'rgba(0,0,0,0.45)',
            zIndex: 1000,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div
            onClick={e => e.stopPropagation()}
            style={{
              background: '#fff',
              borderRadius: 10,
              padding: '28px 32px',
              width: 480,
              maxWidth: '95vw',
              boxShadow: '0 20px 40px rgba(0,0,0,0.18)',
            }}
          >
            <h3 style={{ fontSize: 16, fontWeight: 700, color: '#111827', marginBottom: 20 }}>
              Finalize & Export — {approvedBols.length} approved record{approvedBols.length !== 1 ? 's' : ''}
            </h3>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 24 }}>
              <div style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '10px 14px',
                background: '#f0fdf4',
                border: '1px solid #bbf7d0',
                borderRadius: 6,
                fontSize: 13,
                color: '#166534',
              }}>
                <span style={{ fontWeight: 700, fontSize: 16 }}>✓</span>
                <span><strong>{bCount}</strong> record{bCount !== 1 ? 's' : ''} have BOL numbers — ready for accounting</span>
              </div>

              {sidCount > 0 && (
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 10,
                  padding: '10px 14px',
                  background: sidExportedThisSession ? '#f0fdf4' : '#fffbeb',
                  border: `1px solid ${sidExportedThisSession ? '#bbf7d0' : '#fcd34d'}`,
                  borderRadius: 6,
                  fontSize: 13,
                  color: sidExportedThisSession ? '#166534' : '#92400e',
                }}>
                  <span style={{ fontWeight: 700, fontSize: 16 }}>{sidExportedThisSession ? '✓' : '⚠'}</span>
                  <span>
                    <strong>{sidCount}</strong> record{sidCount !== 1 ? 's' : ''} need SID export first (no BOL number yet)
                    {sidExportedThisSession ? ' — SID exported' : ''}
                  </span>
                </div>
              )}
            </div>

            {sidCount > 0 && !sidExportedThisSession && (
              <div style={{ marginBottom: 16 }}>
                <button
                  onClick={onExportProphecy}
                  disabled={sidLoading}
                  style={{
                    display: 'block',
                    width: '100%',
                    background: sidLoading ? '#e5e7eb' : '#f9fafb',
                    color: sidLoading ? '#9ca3af' : '#374151',
                    border: '1px solid #d1d5db',
                    borderRadius: 6,
                    padding: '10px 0',
                    fontWeight: 600,
                    fontSize: 13,
                    cursor: sidLoading ? 'not-allowed' : 'pointer',
                    textAlign: 'center',
                  }}
                >
                  {sidLoading ? 'Generating…' : `Export to Prophecy (${sidCount})`}
                </button>
                <p style={{ fontSize: 12, color: '#6b7280', marginTop: 6, textAlign: 'center' }}>
                  Complete SID export before sending to accounting.
                </p>
              </div>
            )}

            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <button
                onClick={() => setShowModal(false)}
                style={{
                  background: '#fff',
                  color: '#6b7280',
                  border: '1px solid #d1d5db',
                  borderRadius: 5,
                  padding: '8px 18px',
                  fontSize: 13,
                  cursor: 'pointer',
                }}
              >
                Cancel
              </button>
              <button
                onClick={() => { setShowModal(false); onConfirmSend(); }}
                disabled={!canSend || sendLoading}
                title={!canSend ? 'Export SID to Prophecy first' : undefined}
                style={{
                  background: canSend ? '#2D6A4F' : '#9ca3af',
                  color: '#fff',
                  border: 'none',
                  borderRadius: 5,
                  padding: '8px 18px',
                  fontWeight: 600,
                  fontSize: 13,
                  cursor: canSend ? 'pointer' : 'not-allowed',
                  opacity: sendLoading ? 0.7 : 1,
                }}
              >
                Send to Accounting ({approvedBols.length})
              </button>
            </div>
          </div>
        </div>
      )}

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
                <th style={TH}>Invoice Sender</th>
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
                    <td style={TD}>
                      {bol.bol_number ?? <span style={{ color: '#9ca3af' }}>—</span>}
                      {bol.is_third_party && (
                        <span style={{
                          display: 'inline-block',
                          marginLeft: bol.bol_number ? 6 : 4,
                          background: '#fff7ed',
                          color: '#c2410c',
                          border: '1px solid #fed7aa',
                          borderRadius: 3,
                          padding: '1px 6px',
                          fontSize: 10,
                          fontWeight: 700,
                          letterSpacing: '0.04em',
                          verticalAlign: 'middle',
                        }}>3RD PARTY</span>
                      )}
                    </td>
                    <td style={TD_R}>{fmtNum(bol.technique_weight)}</td>
                    <td style={TD_R}>{fmtNum(bol.technique_pallets)}</td>
                    <td style={TD_R}>{fmtNum(bol.technique_pcs)}</td>
                    <td style={{ ...TD, color: '#6b7280', fontSize: 12 }}>{bol.invoice_email_sender || '—'}</td>
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
