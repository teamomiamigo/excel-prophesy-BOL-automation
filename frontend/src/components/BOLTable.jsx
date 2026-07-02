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

function TableHead({ allSelected, someSelected, onToggleSelectAll }) {
  return (
    <thead>
      <tr>
        <th rowSpan={2} style={{ ...TH_STYLE, textAlign: 'center', width: 32 }}>
          <input
            type="checkbox"
            checked={allSelected}
            ref={el => { if (el) el.indeterminate = !allSelected && someSelected; }}
            onChange={onToggleSelectAll}
            title="Select all visible rows"
          />
        </th>
        <th colSpan={4} style={TH_STYLE} />
        <th colSpan={3} style={{ ...TH_GROUP, borderLeft: '2px solid #404040' }}>Technique</th>
        <th colSpan={3} style={{ ...TH_GROUP, borderLeft: '1px solid #404040' }}>Invoice (ALG)</th>
        <th colSpan={3} style={{ ...TH_GROUP, borderLeft: '1px solid #404040' }}>Diff</th>
        <th colSpan={4} style={TH_STYLE} />
      </tr>
      <tr>
        <th style={TH_STYLE}>Trip</th>
        <th style={TH_STYLE}>Manifest</th>
        <th style={TH_STYLE}>BOL</th>
        <th style={TH_STYLE}>Order #</th>
        <th style={{ ...TH_STYLE, textAlign: 'right', borderLeft: '2px solid #333' }}>Wgt</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>Pal</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>PCS</th>
        <th style={{ ...TH_STYLE, textAlign: 'right', borderLeft: '1px solid #333' }}>Wgt</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>Pal</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>PCS</th>
        <th style={{ ...TH_STYLE, textAlign: 'right', borderLeft: '1px solid #333' }}>ΔWgt</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>ΔPal</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>ΔPCS</th>
        <th style={TH_STYLE}>Invoice Sender</th>
        <th style={TH_STYLE}>Invoice #</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>Calc Cost</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>Amount</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>Cost %</th>
        <th style={TH_STYLE}>Notes</th>
        <th style={{ ...TH_STYLE, textAlign: 'center' }}>Actions</th>
      </tr>
    </thead>
  );
}

export default function BOLTable({
  bols, loading, approvingId, unflaggingId, markingThirdPartyId, ignoringId, exportingSidId, checkingBolId,
  filterText, onFilterChange, selectedIds, onToggleSelect, onToggleSelectAll,
  onApprove, onFlagOpen, onUnflag, onNotesUpdate, onMarkThirdParty, onReassignOpen, onIgnore, onExportSid, onCheckBol,
}) {
  const lower = (filterText || '').toLowerCase();
  const matchesBol = b => !filterText || [
    b.technique_trip, b.manifest, b.invoice_number, b.inv_job_number,
    b.bol_number != null ? String(b.bol_number) : '',
  ].some(v => (v || '').toLowerCase().includes(lower));

  // One flat table, no category grouping — sorted by effective date (invoice date when
  // known, otherwise when we first saw the record). A proper user-facing sort (issue #33)
  // will replace this; this is just a sensible default until then.
  const effectiveDate = b => b.invoice_sent_at || b.created_at;
  const visibleBols = bols
    .filter(matchesBol)
    .slice()
    .sort((a, b) => new Date(effectiveDate(a)) - new Date(effectiveDate(b)));
  const totalVisible = visibleBols.length;
  const visibleIds = visibleBols.map(b => b.id);
  const allSelected = visibleIds.length > 0 && visibleIds.every(id => selectedIds.has(id));
  const someSelected = visibleIds.some(id => selectedIds.has(id));

  function rowProps(bol) {
    return {
      key: bol.id,
      bol,
      isApproving:         approvingId         === bol.id,
      isUnflagging:        unflaggingId        === bol.id,
      isMarkingThirdParty: markingThirdPartyId === bol.id,
      isIgnoring:          ignoringId          === bol.id,
      isExportingSid:      exportingSidId      === bol.id,
      isCheckingBol:       checkingBolId       === bol.id,
      isSelected:          selectedIds.has(bol.id),
      onToggleSelect:      () => onToggleSelect(bol.id),
      onApprove:           () => onApprove(bol.id),
      onFlagOpen:          () => onFlagOpen(bol),
      onUnflag:            () => onUnflag(bol.id),
      onNotesUpdate:       notes => onNotesUpdate(bol.id, notes),
      onMarkThirdParty:    () => onMarkThirdParty(bol.id),
      onReassignOpen:      onReassignOpen,
      onIgnore:            onIgnore,
      onExportSid:         () => onExportSid(bol.id),
      onCheckBol:          () => onCheckBol(bol.id),
    };
  }

  return (
    <section style={{ marginBottom: 28 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <h2 style={{ fontSize: 14, fontWeight: 600, color: '#374151' }}>
          Pending Review ({bols.filter(b => b.status === 'pending').length})
          &nbsp;·&nbsp;
          Flagged ({bols.filter(b => b.status === 'flagged').length})
        </h2>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input
            placeholder="Filter by trip, manifest, invoice #, job #, or BOL…"
            value={filterText}
            onChange={e => onFilterChange(e.target.value)}
            style={{
              border: '1px solid #d1d5db',
              borderRadius: 5,
              padding: '5px 10px',
              fontSize: 12,
              width: 320,
              outline: 'none',
            }}
          />
          {filterText && (
            <button
              onClick={() => onFilterChange('')}
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
        </div>
      </div>

      {loading ? (
        <div style={{ padding: 32, textAlign: 'center', color: '#9ca3af' }}>Loading records…</div>
      ) : totalVisible === 0 ? (
        <div style={{
          padding: 32,
          textAlign: 'center',
          color: '#6b7280',
          background: '#fff',
          borderRadius: 8,
          border: '1px solid #e5e7eb',
        }}>
          {filterText ? `No records match "${filterText}"` : 'No pending records — all caught up!'}
        </div>
      ) : (
        <div style={{ overflowX: 'auto', borderRadius: 8, border: '1px solid #e5e7eb', marginBottom: 12 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff' }}>
            <TableHead
              allSelected={allSelected}
              someSelected={someSelected}
              onToggleSelectAll={() => onToggleSelectAll(visibleIds)}
            />
            <tbody>
              {visibleBols.map(bol => <BOLRow {...rowProps(bol)} />)}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
