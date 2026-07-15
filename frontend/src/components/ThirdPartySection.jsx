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

export default function ThirdPartySection({ bols, unmarkingThirdPartyId, movingToLogLoading, onUnmark, onMoveAllToLog }) {
  if (!bols || bols.length === 0) return null;

  return (
    <section style={{ marginBottom: 28 }}>
      <details>
        <summary style={{
          cursor: 'pointer',
          padding: '8px 14px',
          background: '#fff7ed',
          border: '1px solid #fed7aa',
          borderRadius: 8,
          fontSize: 13,
          fontWeight: 600,
          color: '#c2410c',
          userSelect: 'none',
          listStyle: 'none',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
        }}>
          <span>▸ Third Party — {bols.length} record{bols.length !== 1 ? 's' : ''} (customer pays freight directly — excluded from Prophecy export)</span>
          <button
            onClick={(e) => { e.stopPropagation(); onMoveAllToLog(); }}
            disabled={movingToLogLoading}
            title="Approve and send all third-party records directly to the log"
            style={{
              background: movingToLogLoading ? '#d1fae5' : '#2D6A4F',
              color: movingToLogLoading ? '#065f46' : '#fff',
              border: 'none',
              borderRadius: 5,
              padding: '4px 12px',
              fontSize: 12,
              fontWeight: 700,
              cursor: movingToLogLoading ? 'not-allowed' : 'pointer',
              opacity: movingToLogLoading ? 0.7 : 1,
              whiteSpace: 'nowrap',
            }}
          >
            {movingToLogLoading ? 'Moving…' : `Move All to Log (${bols.length})`}
          </button>
        </summary>
        <div style={{ overflowX: 'auto', borderRadius: '0 0 8px 8px', border: '1px solid #fed7aa', borderTop: 'none' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff' }}>
            <thead>
              <tr>
                <th style={TH}>Trip</th>
                <th style={TH}>Manifest</th>
                <th style={TH}>BOL</th>
                <th style={{ ...TH, textAlign: 'right' }}>Wgt</th>
                <th style={{ ...TH, textAlign: 'right' }}>Pallets</th>
                <th style={{ ...TH, textAlign: 'right' }}>PCS</th>
                <th style={{ ...TH, textAlign: 'right' }}>Calc Cost</th>
                <th style={TH}>Notes</th>
                <th style={{ ...TH, textAlign: 'center' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {bols.map(bol => {
                const isUnmarking = unmarkingThirdPartyId === bol.id;
                return (
                  <tr key={bol.id} style={{ background: '#fffaf5' }}>
                    <td style={TD}>{bol.technique_trip || <span style={{ color: '#d1d5db' }}>—</span>}</td>
                    <td style={TD}>{bol.manifest || <span style={{ color: '#d1d5db' }}>—</span>}</td>
                    <td style={TD}>{bol.bol_number ?? <span style={{ color: '#9ca3af' }}>pending</span>}</td>
                    <td style={TD_R}>{fmtNum(bol.technique_weight)}</td>
                    <td style={TD_R}>{fmtNum(bol.technique_pallets)}</td>
                    <td style={TD_R}>{fmtNum(bol.technique_pcs)}</td>
                    <td style={TD_R}>{fmtMoney(bol.access_prog)}</td>
                    <td style={{ ...TD, color: '#6b7280', fontSize: 12 }}>{bol.notes || '—'}</td>
                    <td style={{ ...TD, textAlign: 'center' }}>
                      <div style={{ display: 'flex', gap: 6, justifyContent: 'center' }}>
                        <button
                          onClick={() => onUnmark(bol.id)}
                          disabled={isUnmarking}
                          title="Move back to pending review queue"
                          style={{
                            background: '#fff',
                            color: '#6b7280',
                            border: '1px solid #d1d5db',
                            borderRadius: 4,
                            padding: '4px 10px',
                            fontSize: 12,
                            fontWeight: 600,
                            cursor: isUnmarking ? 'not-allowed' : 'pointer',
                            opacity: isUnmarking ? 0.6 : 1,
                          }}
                        >
                          {isUnmarking ? '…' : '↩ Move to Pending'}
                        </button>
                      </div>
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
