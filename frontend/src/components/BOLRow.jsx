import { useState, useRef, useEffect } from 'react';

// ---------------------------------------------------------------------------
// Cost % variance logic — primary metric (amount / access_prog)
// Green: within 3% | Orange: 3–6% off | Red: >6% off
// ---------------------------------------------------------------------------
function getCostPctStyle(costPct) {
  if (costPct == null) return { color: '#9ca3af' };
  const deviation = Math.abs(costPct * 100 - 100);
  if (deviation < 3) return { color: '#16a34a', fontWeight: 600 };
  if (deviation < 6) return { color: '#ea580c', fontWeight: 600 };
  return              { color: '#dc2626', fontWeight: 700 };
}

function formatCostPct(costPct) {
  if (costPct == null) return 'N/A';
  return `${(costPct * 100).toFixed(2)}%`;
}

function fmtMoney(val) {
  if (val == null) return '—';
  return `$${parseFloat(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtNum(val) {
  if (val == null) return '—';
  return parseInt(val).toLocaleString('en-US');
}

function fmtDiff(val) {
  if (val == null) return '—';
  const n = parseInt(val);
  return n > 0 ? `+${n.toLocaleString('en-US')}` : n.toLocaleString('en-US');
}


const TD = {
  padding: '8px 10px',
  borderBottom: '1px solid #f3f4f6',
  whiteSpace: 'nowrap',
  fontSize: 13,
};

const TD_R = { ...TD, textAlign: 'right' };

export default function BOLRow({ bol, isApproving, isUnflagging, isMarkingThirdParty, isIgnoring, isExportingSid, isCheckingBol, isSelected, onApprove, onFlagOpen, onUnflag, onNotesUpdate, onMarkThirdParty, onReassignOpen, onIgnore, onExportSid, onCheckBol, onToggleSelect }) {
  const [hovered, setHovered] = useState(false);
  const [notesValue, setNotesValue] = useState(bol.notes || '');
  const [saveFlash, setSaveFlash] = useState(false);
  const debounceRef = useRef(null);
  const isFlagged = bol.status === 'flagged';

  // Sync external prop changes (e.g. after invoice upload refreshes data)
  useEffect(() => {
    setNotesValue(bol.notes || '');
  }, [bol.notes]);

  const isIgnored = bol.is_ignored;
  const rowBg = isIgnored
    ? '#f9fafb'
    : isFlagged
    ? '#fffbeb'
    : hovered
    ? '#f9fafb'
    : '#fff';

  function handleNotesChange(e) {
    const val = e.target.value;
    setNotesValue(val);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      await onNotesUpdate(val);
      setSaveFlash(true);
      setTimeout(() => setSaveFlash(false), 1000);
    }, 500);
  }

  return (
    <tr
      style={{ background: rowBg, transition: 'background 0.1s', opacity: isIgnored ? 0.45 : 1 }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {/* Bulk-select checkbox (issue #32) */}
      <td style={{ ...TD, textAlign: 'center' }}>
        <input type="checkbox" checked={!!isSelected} onChange={onToggleSelect} />
      </td>

      {/* Identity */}
      <td style={TD}>{bol.technique_trip || <span style={{ color: '#d1d5db' }}>—</span>}</td>
      <td style={TD}>
        {bol.manifest
          ? <span style={bol.manifest.startsWith('CM_')
              ? { color: '#7c3aed', fontWeight: 600 }
              : {}}>
              {bol.manifest}
            </span>
          : <span style={{ color: '#d1d5db' }}>—</span>
        }
      </td>
      <td style={TD}>{bol.bol_number ?? <span style={{ color: '#d1d5db' }}>pending</span>}</td>
      <td style={TD}>
        {bol.inv_job_number
          ? <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{bol.inv_job_number}</span>
          : <span style={{ color: '#d1d5db' }}>—</span>
        }
      </td>

      {/* Technique quantities — substituted with Prophecy (indigo + P marker) for Wolf/311 rows */}
      {(() => {
        const isP = !bol.technique_trip && (bol.prophecy_weight != null || bol.prophecy_pallets != null);
        const wgt = isP ? bol.prophecy_weight  : bol.technique_weight;
        const pal = isP ? bol.prophecy_pallets : bol.technique_pallets;
        const pcs = isP ? bol.prophecy_pcs     : bol.technique_pcs;
        const P = isP ? <sup style={{ fontSize: 9, marginLeft: 2, opacity: 0.7 }}>P</sup> : null;
        return (
          <>
            <td style={{ ...TD_R, borderLeft: '2px solid #f3f4f6', ...(isP ? { color: '#6366f1' } : {}) }}>
              {fmtNum(wgt)}{wgt != null ? P : null}
            </td>
            <td style={{ ...TD_R, ...(isP ? { color: '#6366f1' } : {}) }}>
              {fmtNum(pal)}{pal != null ? P : null}
            </td>
            <td style={{ ...TD_R, ...(isP ? { color: '#6366f1' } : {}) }}>
              {fmtNum(pcs)}{pcs != null ? P : null}
            </td>
          </>
        );
      })()}

      {/* ALG invoice quantities — null until CSV is uploaded */}
      <td style={{ ...TD_R, borderLeft: '1px solid #f3f4f6', color: bol.alg_weight == null ? '#d1d5db' : undefined }}>
        {fmtNum(bol.alg_weight)}
      </td>
      <td style={{ ...TD_R, color: bol.alg_pallets == null ? '#d1d5db' : undefined }}>
        {fmtNum(bol.alg_pallets)}
      </td>
      <td style={{ ...TD_R, color: bol.alg_pcs == null ? '#d1d5db' : undefined }}>
        {fmtNum(bol.alg_pcs)}
      </td>

      {/* Diffs — alg minus technique */}
      <td style={{ ...TD_R, borderLeft: '1px solid #f3f4f6' }}>
        {fmtDiff(bol.weight_diff)}
      </td>
      <td style={TD_R}>
        {fmtDiff(bol.pallet_diff)}
      </td>
      <td style={TD_R}>
        {fmtDiff(bol.pcs_diff)}
      </td>

      {/* Invoice info */}
      <td style={{ ...TD, color: '#6b7280', fontSize: 12, whiteSpace: 'nowrap' }}>
        {bol.invoice_email_sender || <span style={{ color: '#d1d5db' }}>—</span>}
      </td>
      <td style={{ ...TD, fontWeight: 600 }}>
        {bol.invoice_number
          ? <button
              onClick={() => onReassignOpen && onReassignOpen(bol.id)}
              title="Click to reassign this invoice to a different trip"
              style={{ background: 'none', border: 'none', padding: 0, fontWeight: 600, fontSize: 13, cursor: 'pointer', color: '#1e40af', textDecoration: 'underline dotted', textUnderlineOffset: 3 }}
            >
              {bol.invoice_number}
            </button>
          : <span style={{ color: '#d1d5db' }}>—</span>
        }
        {isIgnored && <span style={{ marginLeft: 6, fontSize: 10, background: '#e5e7eb', color: '#6b7280', borderRadius: 3, padding: '1px 5px', fontWeight: 700, letterSpacing: '0.04em' }}>IGNORED</span>}
      </td>
      <td style={TD_R}
        title={bol.base_tariff != null && bol.fsc_pct != null
          ? `Base: ${fmtMoney(bol.base_tariff)} × FSC (${(parseFloat(bol.fsc_pct) * 100).toFixed(1)}%) = ${fmtMoney(bol.access_prog)}`
            + (bol.weight_source_fallback ? ' — estimate uses ALG\'s invoiced weight; our own pallet data was unavailable' : '')
            + (bol.tariff_zone_approximate ? ' — one or more zones used a nearest-zone rate guess, not an exact match' : '')
          : undefined}
      >
        {fmtMoney(bol.access_prog)}
        {(bol.weight_source_fallback || bol.tariff_zone_approximate) && (
          <span style={{ marginLeft: 4, fontSize: 10, background: '#fef3c7', color: '#92400e', borderRadius: 3, padding: '1px 5px', fontWeight: 700, letterSpacing: '0.02em' }}>~EST</span>
        )}
      </td>
      <td style={{ ...TD_R, fontWeight: 600 }}>{fmtMoney(bol.amount)}</td>

      <td style={{ ...TD_R, ...getCostPctStyle(bol.cost_pct) }}>
        {formatCostPct(bol.cost_pct)}
      </td>

      {/* Editable notes with auto-save */}
      <td style={{ ...TD, minWidth: 140 }}>
        <div style={{ position: 'relative' }}>
          <input
            type="text"
            value={notesValue}
            onChange={handleNotesChange}
            placeholder="Add note…"
            style={{
              width: '100%',
              border: saveFlash ? '1px solid #86efac' : '1px solid transparent',
              background: saveFlash ? '#f0fdf4' : 'transparent',
              borderRadius: 3,
              padding: '2px 5px',
              fontSize: 12,
              color: '#4b5563',
              outline: 'none',
              transition: 'border-color 0.2s, background 0.2s',
              boxSizing: 'border-box',
            }}
            onFocus={e => { e.target.style.border = '1px solid #d1d5db'; e.target.style.background = '#fff'; }}
            onBlur={e => {
              if (!saveFlash) {
                e.target.style.border = '1px solid transparent';
                e.target.style.background = 'transparent';
              }
            }}
          />
          {isFlagged && bol.flag_reason && (
            <div style={{ color: '#b45309', fontSize: 11, marginTop: 2 }}>⚑ {bol.flag_reason}</div>
          )}
        </div>
      </td>

      {/* Actions — fixed 5-slot grid so every row has identical column width */}
      <td style={{ ...TD, textAlign: 'center' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '80px 36px 36px 36px 42px', gap: 4, alignItems: 'center', justifyContent: 'center' }}>
          {/* Slot 1: Approve */}
          <button
            onClick={onApprove}
            disabled={isApproving}
            title="Approve this record"
            style={{
              background: isApproving ? '#d1fae5' : '#2D6A4F',
              color: isApproving ? '#065f46' : '#fff',
              border: 'none',
              borderRadius: 4,
              padding: '4px 0',
              width: '100%',
              fontSize: 12,
              fontWeight: 600,
              opacity: isApproving ? 0.7 : 1,
              cursor: isApproving ? 'not-allowed' : 'pointer',
            }}
          >
            {isApproving ? '…' : '✓ Approve'}
          </button>
          {/* Slot 2: Flag ↔ Unflag (swaps in place, same slot) */}
          {isFlagged ? (
            <button
              onClick={onUnflag}
              disabled={isUnflagging}
              title="Remove flag and return to pending"
              style={{
                background: '#fff',
                color: '#6b7280',
                border: '1px solid #d1d5db',
                borderRadius: 4,
                padding: '4px 0',
                width: '100%',
                fontSize: 12,
                fontWeight: 600,
                cursor: isUnflagging ? 'not-allowed' : 'pointer',
                opacity: isUnflagging ? 0.6 : 1,
              }}
            >
              {isUnflagging ? '…' : '✕'}
            </button>
          ) : (
            <button
              onClick={onFlagOpen}
              title="Flag this record for review"
              style={{
                background: '#fff7ed',
                color: '#92400e',
                border: '1px solid #fcd34d',
                borderRadius: 4,
                padding: '4px 0',
                width: '100%',
                fontSize: 12,
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              ⚑
            </button>
          )}
          {/* Slot 3: 3P for eligible rows | Ignore link for invoice-only stubs | spacer otherwise */}
          {!bol.amount && !bol.bol_number && !bol.is_third_party ? (
            <button
              onClick={onMarkThirdParty}
              disabled={isMarkingThirdParty}
              title="Mark as third-party — customer pays freight directly"
              style={{
                background: '#fff7ed',
                color: '#c2410c',
                border: '1px solid #fed7aa',
                borderRadius: 4,
                padding: '4px 0',
                width: '100%',
                fontSize: 11,
                fontWeight: 700,
                cursor: isMarkingThirdParty ? 'not-allowed' : 'pointer',
                opacity: isMarkingThirdParty ? 0.6 : 1,
                letterSpacing: '0.02em',
              }}
            >
              {isMarkingThirdParty ? '…' : '3P'}
            </button>
          ) : bol.technique_trip == null && bol.invoice_number ? (
            isIgnored ? (
              <button
                onClick={() => onIgnore && onIgnore(bol.id, false)}
                title="Unignore — restore this record"
                style={{ background: 'none', border: 'none', padding: 0, fontSize: 11, color: '#6b7280', cursor: 'pointer', textDecoration: 'underline' }}
              >
                {isIgnoring ? '…' : 'Unignore'}
              </button>
            ) : (
              <button
                onClick={() => onIgnore && onIgnore(bol.id, true)}
                disabled={isIgnoring}
                title="Ignore — mark as unresolvable, exclude from exports"
                style={{ background: 'none', border: 'none', padding: 0, fontSize: 11, color: '#9ca3af', cursor: isIgnoring ? 'not-allowed' : 'pointer', textDecoration: 'underline' }}
              >
                {isIgnoring ? '…' : 'Ignore'}
              </button>
            )
          ) : (
            <div style={{ width: '100%' }} />
          )}
          {/* Slot 4 + 5: Export to Prophecy / Check BOL — only for pending Type A records
              (no BOL yet, has a manifest, not third-party/ignored) */}
          {bol.needs_sid_export && bol.manifest && !bol.is_third_party && !bol.is_ignored ? (
            <>
              <button
                onClick={onExportSid}
                disabled={isExportingSid}
                title="Export this record's Prophecy SID file (one manifest)"
                style={{
                  background: isExportingSid ? '#dbeafe' : '#eff6ff',
                  color: '#1e40af',
                  border: '1px solid #bfdbfe',
                  borderRadius: 4,
                  padding: '4px 0',
                  width: '100%',
                  fontSize: 11,
                  fontWeight: 700,
                  cursor: isExportingSid ? 'not-allowed' : 'pointer',
                  opacity: isExportingSid ? 0.7 : 1,
                }}
              >
                {isExportingSid ? '…' : 'SID'}
              </button>
              <button
                onClick={onCheckBol}
                disabled={isCheckingBol}
                title="Refresh BOL status and manifest weight/pallets/pieces from Technique"
                style={{
                  background: isCheckingBol ? '#e5e7eb' : '#f9fafb',
                  color: '#374151',
                  border: '1px solid #d1d5db',
                  borderRadius: 4,
                  padding: '4px 0',
                  width: '100%',
                  fontSize: 11,
                  fontWeight: 600,
                  cursor: isCheckingBol ? 'not-allowed' : 'pointer',
                  opacity: isCheckingBol ? 0.7 : 1,
                }}
              >
                {isCheckingBol ? '…' : '↻ BOL'}
              </button>
            </>
          ) : (
            <>
              <div style={{ width: '100%' }} />
              <div style={{ width: '100%' }} />
            </>
          )}
        </div>
      </td>
    </tr>
  );
}
