import { useState, useRef, useEffect } from 'react';

// ---------------------------------------------------------------------------
// Cost % variance logic — primary metric (amount / access_prog)
// Green: within 5% of 100% | Yellow: 5–10% off | Red: >10% off
// ---------------------------------------------------------------------------
function getCostPctStyle(costPct) {
  if (costPct == null) return { color: '#9ca3af' };
  const deviation = Math.abs(costPct * 100 - 100);
  if (deviation < 5)  return { color: '#16a34a', fontWeight: 600 };
  if (deviation < 10) return { color: '#d97706', fontWeight: 600 };
  return               { color: '#dc2626', fontWeight: 700 };
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

// Diff cell color: amber if non-zero, red if >5% of technique value
function getDiffStyle(diff, base) {
  if (diff == null) return {};
  const n = parseInt(diff);
  if (n === 0) return { color: '#9ca3af' };
  const b = base ? parseFloat(base) : 0;
  if (b > 0 && Math.abs(n / b) > 0.05) return { background: '#fee2e2', color: '#991b1b', fontWeight: 600 };
  return { background: '#fef3c7', color: '#92400e' };
}

const TD = {
  padding: '8px 10px',
  borderBottom: '1px solid #f3f4f6',
  whiteSpace: 'nowrap',
  fontSize: 13,
};

const TD_R = { ...TD, textAlign: 'right' };

export default function BOLRow({ bol, isApproving, isUnflagging, onApprove, onFlagOpen, onUnflag, onNotesUpdate }) {
  const [hovered, setHovered] = useState(false);
  const [notesValue, setNotesValue] = useState(bol.notes || '');
  const [saveFlash, setSaveFlash] = useState(false);
  const debounceRef = useRef(null);
  const isFlagged = bol.status === 'flagged';

  // Sync external prop changes (e.g. after invoice upload refreshes data)
  useEffect(() => {
    setNotesValue(bol.notes || '');
  }, [bol.notes]);

  const rowBg = isFlagged
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
      style={{ background: rowBg, transition: 'background 0.1s' }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
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

      {/* Technique quantities */}
      <td style={{ ...TD_R, borderLeft: '2px solid #f3f4f6' }}>{fmtNum(bol.technique_weight)}</td>
      <td style={TD_R}>{fmtNum(bol.technique_pallets)}</td>
      <td style={TD_R}>{fmtNum(bol.technique_pcs)}</td>

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
      <td style={{ ...TD_R, borderLeft: '1px solid #f3f4f6', ...getDiffStyle(bol.weight_diff, bol.technique_weight) }}>
        {fmtDiff(bol.weight_diff)}
      </td>
      <td style={{ ...TD_R, ...getDiffStyle(bol.pallet_diff, bol.technique_pallets) }}>
        {fmtDiff(bol.pallet_diff)}
      </td>
      <td style={{ ...TD_R, ...getDiffStyle(bol.pcs_diff, bol.technique_pcs) }}>
        {fmtDiff(bol.pcs_diff)}
      </td>

      {/* Invoice info */}
      <td style={{ ...TD, fontWeight: 600 }}>{bol.invoice_number || <span style={{ color: '#d1d5db' }}>—</span>}</td>
      <td style={TD_R}>{fmtMoney(bol.access_prog)}</td>
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

      {/* Actions */}
      <td style={{ ...TD, textAlign: 'center' }}>
        <div style={{ display: 'flex', gap: 6, justifyContent: 'center' }}>
          <button
            onClick={onApprove}
            disabled={isApproving}
            title="Approve this record"
            style={{
              background: isApproving ? '#d1fae5' : '#2D6A4F',
              color: isApproving ? '#065f46' : '#fff',
              border: 'none',
              borderRadius: 4,
              padding: '4px 10px',
              fontSize: 12,
              fontWeight: 600,
              opacity: isApproving ? 0.7 : 1,
              cursor: isApproving ? 'not-allowed' : 'pointer',
            }}
          >
            {isApproving ? '…' : '✓ Approve'}
          </button>
          <button
            onClick={onFlagOpen}
            title="Flag this record for review"
            style={{
              background: isFlagged ? '#fef3c7' : '#fff7ed',
              color: '#92400e',
              border: '1px solid #fcd34d',
              borderRadius: 4,
              padding: '4px 10px',
              fontSize: 12,
              fontWeight: 600,
            }}
          >
            ⚑ Flag
          </button>
          {isFlagged && (
            <button
              onClick={onUnflag}
              disabled={isUnflagging}
              title="Remove flag and return to pending"
              style={{
                background: '#fff',
                color: '#6b7280',
                border: '1px solid #d1d5db',
                borderRadius: 4,
                padding: '4px 10px',
                fontSize: 12,
                fontWeight: 600,
                cursor: isUnflagging ? 'not-allowed' : 'pointer',
                opacity: isUnflagging ? 0.6 : 1,
              }}
            >
              {isUnflagging ? '…' : '✕ Unflag'}
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}
