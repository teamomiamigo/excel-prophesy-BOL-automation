import BOLRow from './BOLRow.jsx';

const TH_STYLE = {
  padding: '8px 10px',
  background: '#1A1A1A',
  color: '#fff',
  textAlign: 'left',
  fontSize: 11,
  fontWeight: 600,
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
  whiteSpace: 'nowrap',
  position: 'sticky',
  top: 0,
};

const TH_GROUP = {
  ...TH_STYLE,
  textAlign: 'center',
  background: '#2a2a2a',
  borderBottom: '1px solid #404040',
  fontSize: 10,
  letterSpacing: '0.06em',
  color: '#d1d5db',
};

export default function BOLTable({ bols, loading, approvingId, unflaggingId, onApprove, onFlagOpen, onUnflag, onNotesUpdate }) {
  return (
    <section style={{ marginBottom: 28 }}>
      <h2 style={{ fontSize: 14, fontWeight: 600, color: '#374151', marginBottom: 10 }}>
        Pending Review ({bols.filter(b => b.status === 'pending').length}) &nbsp;·&nbsp; Flagged ({bols.filter(b => b.status === 'flagged').length})
      </h2>

      {loading ? (
        <div style={{ padding: 32, textAlign: 'center', color: '#9ca3af' }}>Loading records…</div>
      ) : bols.length === 0 ? (
        <div style={{
          padding: 32,
          textAlign: 'center',
          color: '#6b7280',
          background: '#fff',
          borderRadius: 8,
          border: '1px solid #e5e7eb',
        }}>
          No pending records — all caught up!
        </div>
      ) : (
        <div style={{ overflowX: 'auto', borderRadius: 8, border: '1px solid #e5e7eb' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff' }}>
            <thead>
              {/* Group header row */}
              <tr>
                <th colSpan={4} style={TH_STYLE} />
                <th colSpan={3} style={{ ...TH_GROUP, borderLeft: '2px solid #404040' }}>Technique</th>
                <th colSpan={3} style={{ ...TH_GROUP, borderLeft: '1px solid #404040' }}>Invoice (ALG)</th>
                <th colSpan={3} style={{ ...TH_GROUP, borderLeft: '1px solid #404040' }}>Diff</th>
                <th colSpan={4} style={TH_STYLE} />
              </tr>
              {/* Column header row */}
              <tr>
                <th style={TH_STYLE}>Trip</th>
                <th style={TH_STYLE}>Manifest</th>
                <th style={TH_STYLE}>BOL</th>
                <th style={TH_STYLE}>Job #</th>
                <th style={{ ...TH_STYLE, textAlign: 'right', borderLeft: '2px solid #333' }}>Wgt</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>Pal</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>PCS</th>
                <th style={{ ...TH_STYLE, textAlign: 'right', borderLeft: '1px solid #333' }}>Wgt</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>Pal</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>PCS</th>
                <th style={{ ...TH_STYLE, textAlign: 'right', borderLeft: '1px solid #333' }}>ΔWgt</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>ΔPal</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>ΔPCS</th>
                <th style={TH_STYLE}>Invoice #</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>Calc Cost</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>Amount</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>Cost %</th>
                <th style={TH_STYLE}>Notes</th>
                <th style={{ ...TH_STYLE, textAlign: 'center' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {bols.map(bol => (
                <BOLRow
                  key={bol.id}
                  bol={bol}
                  isApproving={approvingId === bol.id}
                  isUnflagging={unflaggingId === bol.id}
                  onApprove={() => onApprove(bol.id)}
                  onFlagOpen={() => onFlagOpen(bol)}
                  onUnflag={() => onUnflag(bol.id)}
                  onNotesUpdate={(notes) => onNotesUpdate(bol.id, notes)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
