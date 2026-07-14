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

export default function IgnoredSection({ bols, eligibleCount, unignoringId, bulkLoading, onUnignore, onIgnoreAll }) {
  if ((!bols || bols.length === 0) && !eligibleCount) return null;

  return (
    <section style={{ marginBottom: 28 }}>
      {eligibleCount > 0 && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 6 }}>
          <button
            onClick={onIgnoreAll}
            disabled={bulkLoading}
            title="Ignore every remaining eligible pending record in one click"
            style={{
              background: bulkLoading ? '#e5e7eb' : '#f3f4f6',
              color: bulkLoading ? '#9ca3af' : '#374151',
              border: '1px solid #d1d5db',
              borderRadius: 5,
              padding: '5px 12px',
              fontWeight: 600,
              fontSize: 12,
              cursor: bulkLoading ? 'not-allowed' : 'pointer',
            }}
          >
            {bulkLoading ? 'Ignoring…' : `Ignore All (${eligibleCount})`}
          </button>
        </div>
      )}
      <details>
        <summary style={{
          cursor: 'pointer',
          padding: '8px 14px',
          background: '#f3f4f6',
          border: '1px solid #d1d5db',
          borderRadius: 8,
          fontSize: 13,
          fontWeight: 600,
          color: '#374151',
          userSelect: 'none',
          listStyle: 'none',
        }}>
          ▸ Ignored — {bols.length} record{bols.length !== 1 ? 's' : ''} (marked unresolvable — excluded from exports)
        </summary>
        <div style={{ overflowX: 'auto', borderRadius: '0 0 8px 8px', border: '1px solid #d1d5db', borderTop: 'none' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff' }}>
            <thead>
              <tr>
                <th style={TH}>Manifest</th>
                <th style={TH}>Order #</th>
                <th style={TH}>Invoice Sender</th>
                <th style={TH}>Invoice #</th>
                <th style={{ ...TH, textAlign: 'right' }}>Amount</th>
                <th style={{ ...TH, textAlign: 'center' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {bols.map(bol => {
                const isUnignoring = unignoringId === bol.id;
                return (
                  <tr key={bol.id} style={{ background: '#fafafa' }}>
                    <td style={TD}>
                      {bol.manifest
                        ? <span style={bol.manifest.startsWith('CM_') ? { color: '#7c3aed', fontWeight: 600 } : {}}>{bol.manifest}</span>
                        : <span style={{ color: '#d1d5db' }}>—</span>
                      }
                    </td>
                    <td style={TD}>
                      {bol.inv_job_number
                        ? <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{bol.inv_job_number}</span>
                        : <span style={{ color: '#d1d5db' }}>—</span>
                      }
                    </td>
                    <td style={{ ...TD, color: '#6b7280', fontSize: 12 }}>
                      {bol.invoice_email_sender || <span style={{ color: '#d1d5db' }}>—</span>}
                    </td>
                    <td style={TD}>
                      {bol.invoice_number
                        ? <a
                            href={`/api/invoices/${bol.invoice_number}/file`}
                            target="_blank"
                            rel="noreferrer"
                            title={`Open invoice PDF for ${bol.invoice_number}`}
                            style={{ fontSize: 12, color: '#1e40af', textDecoration: 'none', background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 4, padding: '2px 7px', fontWeight: 600 }}
                          >
                            {bol.invoice_number}
                          </a>
                        : <span style={{ color: '#d1d5db' }}>—</span>
                      }
                    </td>
                    <td style={TD_R}>{fmtMoney(bol.amount)}</td>
                    <td style={{ ...TD, textAlign: 'center' }}>
                      <button
                        onClick={() => onUnignore(bol.id)}
                        disabled={isUnignoring}
                        title="Unignore — restore this record to pending review"
                        style={{
                          background: '#fff',
                          color: '#6b7280',
                          border: '1px solid #d1d5db',
                          borderRadius: 4,
                          padding: '4px 10px',
                          fontSize: 12,
                          fontWeight: 600,
                          cursor: isUnignoring ? 'not-allowed' : 'pointer',
                          opacity: isUnignoring ? 0.6 : 1,
                        }}
                      >
                        {isUnignoring ? '…' : '↩ Unignore'}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </details>
    </section>
  );
}
