import { useRef } from 'react';
import BOLRow from './BOLRow.jsx';
import useEdgeScroll from '../hooks/useEdgeScroll.js';
import { groupBySender, sortGroupsByRecency } from '../utils/groupBySender.js';

// 1 checkbox column (rowSpan 2) + 20 data columns in the second header row below.
const TOTAL_COLUMNS = 21;

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

const sortableThStyle = { ...TH_STYLE, cursor: 'pointer', userSelect: 'none' };

function sortIndicator(sort, column) {
  if (sort.column !== column) return <span style={{ opacity: 0.35, marginLeft: 4 }}>⇅</span>;
  return <span style={{ marginLeft: 4 }}>{sort.direction === 'asc' ? '▲' : '▼'}</span>;
}

// Sort accessors + comparator (issue #33 — sortable columns)
const SORT_ACCESSORS = {
  trip:     { get: b => b.technique_trip, numeric: false },
  manifest: { get: b => b.manifest,       numeric: false },
  bol:      { get: b => b.bol_number,     numeric: true  },
  invoice:  { get: b => b.invoice_number, numeric: false },
};

function makeComparator(accessor, direction, isNumeric) {
  const dir = direction === 'desc' ? -1 : 1;
  return (a, b) => {
    const av = accessor(a), bv = accessor(b);
    const aNull = av == null, bNull = bv == null;
    if (aNull && bNull) return 0;
    if (aNull) return 1;   // nulls always last, regardless of direction
    if (bNull) return -1;
    return dir * (isNumeric ? av - bv : String(av).localeCompare(String(bv)));
  };
}

// No active column sort -> null, meaning "leave this group's records in their
// incoming order" (whatever GET /api/bols already returned). Superseded the old
// whole-list invoice_sent_at default sort (Phase 7, commit 0ea0a03, 2026-07-22) --
// group order is now handled separately, by sender (see groupOrder below).
function getComparator(sort) {
  if (!sort.column) return null;
  const { get, numeric } = SORT_ACCESSORS[sort.column];
  return makeComparator(get, sort.direction, numeric);
}

function groupOrderIndicator(groupOrder) {
  return <span style={{ marginLeft: 4 }}>{groupOrder === 'asc' ? '▲' : '▼'}</span>;
}

function TableHead({ allSelected, someSelected, onToggleSelectAll, sort, onSort, groupOrder, onToggleGroupOrder }) {
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
        <th colSpan={3} style={{ ...TH_GROUP, borderLeft: '2px solid #404040' }}>SG360</th>
        <th colSpan={3} style={{ ...TH_GROUP, borderLeft: '1px solid #404040' }}>Invoice (ALG)</th>
        <th colSpan={3} style={{ ...TH_GROUP, borderLeft: '1px solid #404040' }}>Diff</th>
        <th colSpan={4} style={TH_STYLE} />
      </tr>
      <tr>
        <th style={sortableThStyle} onClick={() => onSort('trip')} title="Sort by Trip #">Trip{sortIndicator(sort, 'trip')}</th>
        <th style={sortableThStyle} onClick={() => onSort('manifest')} title="Sort by Manifest #">Manifest{sortIndicator(sort, 'manifest')}</th>
        <th style={sortableThStyle} onClick={() => onSort('bol')} title="Sort by BOL #">BOL{sortIndicator(sort, 'bol')}</th>
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
        <th
          style={sortableThStyle}
          onClick={onToggleGroupOrder}
          title={`Sender batches grouped ${groupOrder === 'asc' ? 'oldest' : 'newest'}-first — click to reverse`}
        >
          Invoice Sender{groupOrderIndicator(groupOrder)}
        </th>
        <th style={sortableThStyle} onClick={() => onSort('invoice')} title="Sort by Invoice #">Invoice #{sortIndicator(sort, 'invoice')}</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>Calc Cost</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>Amount</th>
        <th style={{ ...TH_STYLE, textAlign: 'right' }}>Cost %</th>
        <th style={TH_STYLE}>Notes</th>
        <th style={{ ...TH_STYLE, textAlign: 'center', borderLeft: '1px solid #333' }}>Actions</th>
      </tr>
    </thead>
  );
}

export default function BOLTable({
  bols, loading, approvingId, unflaggingId, markingThirdPartyId, markingDoNotPayId, exportingSidId, checkingBolId, retryingMatchId, acknowledgingMismatchId,
  filterText, onFilterChange, selectedIds, onToggleSelect, onToggleSelectAll, sort, onSort, groupOrder, onToggleGroupOrder,
  onApprove, onFlagOpen, onUnflag, onNotesUpdate, onMarkThirdParty, onReassignOpen, onCompareOpen, onAcknowledgeMismatch, onDoNotPay, onExportSid, onCheckBol, onRetryMatch,
}) {
  const lower = (filterText || '').toLowerCase();
  const matchesBol = b => !filterText || [
    b.technique_trip, b.manifest, b.invoice_number, b.inv_job_number,
    b.bol_number != null ? String(b.bol_number) : '',
    b.invoice_email_sender,
  ].some(v => (v || '').toLowerCase().includes(lower));

  // Sender is the highest form of organization: records are grouped into contiguous
  // per-invoice_email_sender blocks (groupBySender), and groups themselves are ordered
  // by recency (sortGroupsByRecency, outer sort, newest-first by default — groupOrder,
  // replacing Phase 7's whole-list oldest-first default, commit 0ea0a03 2026-07-22).
  // Clicking a column header (Trip/Manifest/BOL/Invoice #) only reorders records WITHIN
  // each group (intraGroupComparator, inner sort) — a record can never leave its
  // sender's block regardless of column-sort state. Technique-matched and
  // Prophecy-BOL-matched records group identically since invoice_email_sender is set
  // the same way regardless of match_strategy.
  const filteredBols = bols.filter(matchesBol);
  const orderedGroups = sortGroupsByRecency(groupBySender(filteredBols), groupOrder);
  const intraGroupComparator = getComparator(sort);
  const visibleGroups = orderedGroups.map(g => ({
    ...g,
    records: intraGroupComparator ? g.records.slice().sort(intraGroupComparator) : g.records,
  }));
  const visibleBols = visibleGroups.flatMap(g => g.records);
  const totalVisible = visibleBols.length;
  const visibleIds = visibleBols.map(b => b.id);
  const allSelected = visibleIds.length > 0 && visibleIds.every(id => selectedIds.has(id));
  const someSelected = visibleIds.some(id => selectedIds.has(id));

  const scrollRef = useRef(null);
  const tableRendered = !loading && totalVisible > 0;

  useEdgeScroll(scrollRef, tableRendered);

  function rowProps(bol) {
    return {
      bol,
      isApproving:         approvingId         === bol.id,
      isUnflagging:        unflaggingId        === bol.id,
      isMarkingThirdParty: markingThirdPartyId === bol.id,
      isMarkingDoNotPay:   markingDoNotPayId   === bol.id,
      isExportingSid:      exportingSidId      === bol.id,
      isCheckingBol:       checkingBolId       === bol.id,
      isRetryingMatch:     retryingMatchId     === bol.id,
      isAcknowledgingMismatch: acknowledgingMismatchId === bol.id,
      isSelected:          selectedIds.has(bol.id),
      onToggleSelect:      () => onToggleSelect(bol.id),
      onApprove:           () => onApprove(bol.id),
      onFlagOpen:          () => onFlagOpen(bol),
      onUnflag:            () => onUnflag(bol.id),
      onNotesUpdate:       notes => onNotesUpdate(bol.id, notes),
      onMarkThirdParty:    () => onMarkThirdParty(bol.id),
      onReassignOpen:      onReassignOpen,
      onCompareOpen:       onCompareOpen,
      onAcknowledgeMismatch: () => onAcknowledgeMismatch(bol.id),
      onDoNotPay:          onDoNotPay,
      onExportSid:         () => onExportSid(bol.id),
      onCheckBol:          () => onCheckBol(bol.id),
      onRetryMatch:        () => onRetryMatch(bol.id),
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
            placeholder="Filter by trip, manifest, invoice #, job #, BOL, or sender…"
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
        <div ref={scrollRef} style={{ overflowX: 'auto', borderRadius: 8, border: '1px solid #e5e7eb', marginBottom: 12 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff' }}>
            <TableHead
              allSelected={allSelected}
              someSelected={someSelected}
              onToggleSelectAll={() => onToggleSelectAll(visibleIds)}
              sort={sort}
              onSort={onSort}
              groupOrder={groupOrder}
              onToggleGroupOrder={onToggleGroupOrder}
            />
            {visibleGroups.map((group, idx) => (
              <tbody key={group.key}>
                <tr>
                  <td
                    colSpan={TOTAL_COLUMNS}
                    style={{
                      padding: '5px 10px',
                      fontSize: 11,
                      fontWeight: 700,
                      color: '#374151',
                      background: idx === 0 ? '#f0fdf4' : '#f3f4f6',
                      borderBottom: '1px solid #e5e7eb',
                      borderTop: idx === 0 ? 'none' : '2px solid #e5e7eb',
                    }}
                  >
                    {group.label}
                    <span style={{ fontWeight: 400, color: '#6b7280', marginLeft: 8 }}>
                      · {group.records.length} record{group.records.length === 1 ? '' : 's'}
                    </span>
                  </td>
                </tr>
                {group.records.map(bol => <BOLRow key={bol.id} {...rowProps(bol)} />)}
              </tbody>
            ))}
          </table>
        </div>
      )}
    </section>
  );
}
