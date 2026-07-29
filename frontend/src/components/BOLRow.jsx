import { useState } from 'react';

// do not pay is only for invoice-only stubs with no technique/prophecy match at all
export function isDoNotPayEligible(bol) {
  return bol.technique_trip == null
    && !!bol.invoice_number
    && !(bol.match_strategy === 'invoice_only' && !bol.bol_number);
}

// covers pre-invoice technique records and never-matched invoice-only stubs, but not a normal matched record
export function isThirdPartyEligible(bol) {
  return !bol.is_third_party
    && !bol.bol_number
    && !(bol.technique_trip && bol.amount);
}

// two triggers: an unresolved ambiguous trip, or a bol_number whose own quantities don't fit
const QUANTITY_MISMATCH_THRESHOLD = 0.15; // mirrors backend's _CLOSE_MATCH_THRESHOLD (main.py)

function _relDiff(diffVal, algVal) {
  if (diffVal == null || !algVal) return 0;
  return Math.abs(diffVal) / Math.abs(algVal);
}

export function hasSevereQuantityMismatch(bol) {
  const score = _relDiff(bol.weight_diff, bol.alg_weight)
              + _relDiff(bol.pallet_diff, bol.alg_pallets)
              + _relDiff(bol.pcs_diff, bol.alg_pcs);
  return score > QUANTITY_MISMATCH_THRESHOLD;
}

// a severe mismatch with no ambiguous trip has nothing for Compare to show -- Acknowledge
// just clears the badge once Katie's confirmed the mismatch is expected
export function isMismatchAcknowledgeEligible(bol) {
  return !bol.is_third_party
    && !bol.mismatch_acknowledged
    && hasSevereQuantityMismatch(bol)
    && !(!!bol.is_ambiguous_trip && !bol.bol_number);
}

export function isUnverifiedQuantity(bol) {
  if (bol.is_third_party) return false;
  const ambiguousUnresolved = !!bol.is_ambiguous_trip && !bol.bol_number;
  const severeMismatch = hasSevereQuantityMismatch(bol) && !bol.mismatch_acknowledged;
  return ambiguousUnresolved || severeMismatch;
}

// flagged records must be unflagged first; a record needs a bol_number to approve,
// except third-party records, which structurally never get one
export function isApproveEligible(bol) {
  return bol.status !== 'flagged' && (!!bol.bol_number || bol.is_third_party);
}

// cost % = amount / access_prog. green: within 3% | orange: 3-6% off | red: >6% off
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

// diagnostic only -- detects destinations billed a separate minimum charge per pallet
// row instead of once for the combined shipment; doesn't change access_prog itself
const MAX_DUPLICATE_LINES = 5;

function analyzeDuplicateDestinations(pallets) {
  const byDest = {};
  for (const p of pallets) {
    if (!p.dest_id) continue;
    if (!byDest[p.dest_id]) byDest[p.dest_id] = [];
    byDest[p.dest_id].push(p);
  }
  // fsc_pct is constant for the whole invoice -- derive the multiplier from any one
  // row instead of threading fsc_pct through this component separately.
  const sample = pallets.find(p => p.base);
  const fscMultiplier = sample ? sample.with_fsc / sample.base : 1;

  const dupes = Object.entries(byDest)
    .filter(([, rows]) => rows.length > 1)
    .map(([destId, rows]) => {
      const totalWeight = rows.reduce((sum, r) => sum + (r.weight || 0), 0);
      const currentBase = rows.reduce((sum, r) => sum + (r.base || 0), 0);
      const rate = rows[0].rate_used;
      const mc1 = rows[0].mc1_used;
      const combinedBase = rate != null && mc1 != null
        ? Math.max(Math.round((rate * totalWeight / 100) * 100) / 100, mc1)
        : null;
      const excessBase = combinedBase != null ? currentBase - combinedBase : null;
      return {
        destId,
        zip3: rows[0].zip3,
        rows,
        mc1,
        excessWithFsc: excessBase != null ? excessBase * fscMultiplier : null,
      };
    })
    .filter(d => d.excessWithFsc != null && d.excessWithFsc > 0.005)
    .sort((a, b) => b.excessWithFsc - a.excessWithFsc);

  const totalExcessWithFsc = dupes.reduce((sum, d) => sum + d.excessWithFsc, 0);
  return { dupes, totalExcessWithFsc };
}

// cost summary popover, shown on hover over the Calc Cost cell -- a rollup, not a
// per-pallet table, since a real invoice can carry 100+ line items
function CostBreakdownPopover({ data }) {
  if (data === 'loading') {
    return (
      <div style={POPOVER_STYLE}>
        <div style={{ color: '#6b7280' }}>Checking…</div>
      </div>
    );
  }
  if (!data || data._error) {
    return (
      <div style={POPOVER_STYLE}>
        <div style={{ color: '#6b7280' }}>{(data && data._error) || 'Cost check failed — request error.'}</div>
      </div>
    );
  }
  const pallets = data.pallets || [];
  const total = pallets.length;
  const noRate = pallets.filter(p => p.rate_source === 'none').length;
  const approxRate = pallets.filter(p => p.rate_source === 'legacy_tariff_rates').length;
  const uncertainMin = pallets.filter(p => p.mc1_source && p.mc1_source !== 'alg_tariff_rates').length;
  const floored = pallets.filter(p => p.floored).length;
  const clean = noRate === 0 && approxRate === 0 && uncertainMin === 0;
  const { dupes, totalExcessWithFsc } = analyzeDuplicateDestinations(pallets);
  const shownDupes = dupes.slice(0, MAX_DUPLICATE_LINES);
  const hiddenDupeCount = dupes.length - shownDupes.length;

  return (
    <div style={{ ...POPOVER_STYLE, width: dupes.length > 0 ? 340 : 300 }}>
      <div style={{ fontWeight: 700, marginBottom: 4 }}>
        Cost check — {total} pallet{total === 1 ? '' : 's'}
      </div>
      {clean ? (
        <div style={{ color: '#16a34a' }}>All pallets priced via a confirmed rate + minimum charge.</div>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 16, color: '#92400e' }}>
          {noRate > 0 && <li>{noRate} pallet{noRate === 1 ? '' : 's'} had no rate found at all — dropped from the total.</li>}
          {approxRate > 0 && <li>{approxRate} pallet{approxRate === 1 ? '' : 's'} priced via an approximate (nearest-zone) rate.</li>}
          {uncertainMin > 0 && <li>{uncertainMin} pallet{uncertainMin === 1 ? '' : 's'}' minimum charge couldn't be confirmed against alg_tariff_rates.</li>}
        </ul>
      )}
      {floored > 0 && (
        <div style={{ color: '#6b7280', marginTop: 4 }}>{floored} pallet{floored === 1 ? '' : 's'} hit a minimum-charge floor.</div>
      )}
      {dupes.length > 0 && (
        <div style={{ marginTop: 6, paddingTop: 6, borderTop: '1px solid #e5e7eb' }}>
          <div style={{ fontWeight: 700, color: '#b45309' }}>
            ⚠ What happened: {dupes.length} destination{dupes.length === 1 ? '' : 's'} received more than one pallet
            record and got billed a separate minimum charge for each — ≈${totalExcessWithFsc.toFixed(2)} of this
            load's cost comes from that.
          </div>
          <ul style={{ margin: '4px 0 0', paddingLeft: 16, color: '#57534e' }}>
            {shownDupes.map(d => (
              <li key={d.destId} style={{ marginBottom: 3 }}>
                <strong>{d.destId}</strong> (zip {d.zip3}) — {d.rows.length} pallets
                ({d.rows.map(r => `${r.weight}lb`).join(' + ')}), ${d.mc1?.toFixed(2)} minimum each
                → excess ≈ ${d.excessWithFsc.toFixed(2)}
              </li>
            ))}
          </ul>
          {hiddenDupeCount > 0 && (
            <div style={{ color: '#9ca3af', marginTop: 2 }}>+ {hiddenDupeCount} more destination{hiddenDupeCount === 1 ? '' : 's'} with the same pattern</div>
          )}
        </div>
      )}
    </div>
  );
}

const POPOVER_STYLE = {
  position: 'absolute',
  top: '100%',
  right: 0,
  marginTop: 4,
  background: '#fff',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  boxShadow: '0 4px 12px rgba(0,0,0,0.12)',
  padding: 10,
  fontSize: 11,
  whiteSpace: 'normal',
  zIndex: 20,
  width: 300,
  textAlign: 'left',
};


const TD = {
  padding: '8px 10px',
  borderBottom: '1px solid #f3f4f6',
  whiteSpace: 'nowrap',
  fontSize: 13,
};

const TD_R = { ...TD, textAlign: 'right' };

// Actions column: small neutral square icon button (Flag toggle)
const ICON_BTN = {
  width: 26,
  height: 26,
  border: '1px solid #e5e7eb',
  borderRadius: 4,
  background: '#fff',
  fontSize: 12,
  fontWeight: 600,
  cursor: 'pointer',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 0,
};

// Actions column: fixed-size empty slot — same footprint as a button, reads as "nothing here"
const PLACEHOLDER = { width: '100%', height: 26 };

export default function BOLRow({ bol, isApproving, isUnflagging, isMarkingThirdParty, isMarkingDoNotPay, isExportingSid, isCheckingBol, isRetryingMatch, isAcknowledgingMismatch, isSelected, onApprove, onFlagOpen, onUnflag, onNotesUpdate, onMarkThirdParty, onReassignOpen, onCompareOpen, onAcknowledgeMismatch, onDoNotPay, onExportSid, onCheckBol, onRetryMatch, onToggleSelect }) {
  const [hovered, setHovered] = useState(false);
  const [costHovered, setCostHovered] = useState(false);
  const [costBreakdown, setCostBreakdown] = useState(null); // cached once fetched: null | 'loading' | { _error } | {...}
  const [editingNotes, setEditingNotes] = useState(false);
  const [notesDraft, setNotesDraft] = useState('');
  const isFlagged = bol.status === 'flagged';
  const approveEligible = isApproveEligible(bol);

  function startEditingNotes() {
    setNotesDraft(bol.notes || '');
    setEditingNotes(true);
  }

  function handleNotesBlur() {
    setEditingNotes(false);
    const trimmed = notesDraft.trim();
    if (trimmed !== (bol.notes || '')) {
      onNotesUpdate(trimmed);
    }
  }

  function handleNotesKeyDown(e) {
    if (e.key === 'Escape') {
      setNotesDraft(bol.notes || '');
      e.target.blur();
    }
  }

  function handleCostEnter() {
    setCostHovered(true);
    if (costBreakdown == null && bol.invoice_number) {
      setCostBreakdown('loading');
      fetch(`/api/bols/${bol.id}/cost-breakdown`)
        .then(async res => {
          const data = await res.json().catch(() => ({}));
          if (!res.ok) { setCostBreakdown({ _error: data.detail || `HTTP ${res.status}` }); return; }
          setCostBreakdown(data);
        })
        .catch(() => setCostBreakdown({ _error: 'Cost check failed — request error.' }));
    }
  }
  // a never-matched stub -- otherwise this could sit forever, so both Retry and 3P are offered
  const isUnresolvedInvoiceOnly = bol.technique_trip == null && bol.match_strategy === 'invoice_only' && !bol.bol_number;

  const rowBg = isFlagged
    ? '#fffbeb'
    : hovered
    ? '#f9fafb'
    : '#fff';

  return (
    <tr
      style={{ background: rowBg, transition: 'background 0.1s' }}
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

      {/* technique quantities, substituted with prophecy (P marker) for Wolf/311 rows */}
      {(() => {
        const isP = !bol.technique_trip && (bol.prophecy_weight != null || bol.prophecy_pallets != null);
        const hasNoBaseline = !bol.technique_trip && bol.prophecy_weight == null && bol.prophecy_pallets == null;
        const wgt = hasNoBaseline ? null : (isP ? bol.prophecy_weight  : bol.technique_weight);
        const pal = hasNoBaseline ? null : (isP ? bol.prophecy_pallets : bol.technique_pallets);
        const pcs = hasNoBaseline ? null : (isP ? bol.prophecy_pcs     : bol.technique_pcs);
        const P = isP ? <sup style={{ fontSize: 9, marginLeft: 2, opacity: 0.85, color: '#6366f1', fontWeight: 700 }}>P</sup> : null;
        const unverified = !isP && isUnverifiedQuantity(bol);
        return (
          <>
            <td
              style={{ ...TD_R, borderLeft: '2px solid #f3f4f6' }}
              title={unverified
                ? 'This trip has multiple manifests in Technique — quantities are provisional until Katie confirms which manifest is billable (BOL created or marked third-party).'
                : undefined}
            >
              {fmtNum(wgt)}{wgt != null ? P : null}
              {unverified && (
                <span style={{ marginLeft: 4, fontSize: 10, background: '#fef3c7', color: '#92400e', borderRadius: 3, padding: '1px 5px', fontWeight: 700, letterSpacing: '0.02em' }}>~UNVERIFIED</span>
              )}
            </td>
            <td style={TD_R}>
              {fmtNum(pal)}{pal != null ? P : null}
            </td>
            <td style={TD_R}>
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
      <td style={{ ...TD, fontWeight: 600 }}>
        {bol.invoice_number
          ? <span style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
              {(bol.invoice_number || '').split(',').map(z => z.trim()).filter(Boolean).map(z => (
                <a
                  key={z}
                  href={`/api/invoices/${z}/file`}
                  target="_blank"
                  rel="noreferrer"
                  title={`Open invoice PDF for ${z}`}
                  style={{ fontSize: 12, color: '#1e40af', textDecoration: 'none', background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 4, padding: '2px 7px', fontWeight: 600, whiteSpace: 'nowrap' }}
                >
                  {z}
                </a>
              ))}
              <button
                onClick={() => onReassignOpen && onReassignOpen(bol.id)}
                title="Reassign invoice to a different trip"
                style={{ background: 'none', border: 'none', padding: '0 2px', cursor: 'pointer', color: '#9ca3af', fontSize: 13, lineHeight: 1 }}
              >
                ↔
              </button>
            </span>
          : <span style={{ color: '#d1d5db' }}>—</span>
        }
      </td>
      <td style={{ ...TD_R, position: 'relative' }}
        title={bol.base_tariff != null && bol.fsc_pct != null
          ? `Base: ${fmtMoney(bol.base_tariff)} × FSC (${(parseFloat(bol.fsc_pct) * 100).toFixed(1)}%) = ${fmtMoney(bol.access_prog)}`
            + (bol.weight_source_fallback ? ' — estimate uses ALG\'s invoiced weight; our own pallet data was unavailable' : '')
            + (bol.tariff_zone_approximate ? ' — one or more zones used a nearest-zone rate guess, not an exact match' : '')
            + (bol.min_charge_uncertain ? ' — one or more zones\' minimum-charge floor could not be confirmed against alg_tariff_rates; hover for details' : '')
          : undefined}
        onMouseEnter={handleCostEnter}
        onMouseLeave={() => setCostHovered(false)}
      >
        {fmtMoney(bol.access_prog)}
        {(bol.weight_source_fallback || bol.tariff_zone_approximate || bol.min_charge_uncertain) && (
          <span style={{ marginLeft: 4, fontSize: 10, background: '#fef3c7', color: '#92400e', borderRadius: 3, padding: '1px 5px', fontWeight: 700, letterSpacing: '0.02em' }}>~EST</span>
        )}
        {costHovered && bol.invoice_number && <CostBreakdownPopover data={costBreakdown} />}
      </td>
      <td style={{ ...TD_R, fontWeight: 600 }}>{fmtMoney(bol.amount)}</td>

      <td style={{ ...TD_R, ...getCostPctStyle(bol.cost_pct) }}>
        {formatCostPct(bol.cost_pct)}
      </td>

      {/* Notes — click to edit inline, saves on blur */}
      <td style={{ ...TD, minWidth: 140, maxWidth: 200, whiteSpace: 'normal' }}>
        {editingNotes ? (
          <textarea
            autoFocus
            value={notesDraft}
            onChange={e => setNotesDraft(e.target.value)}
            onBlur={handleNotesBlur}
            onKeyDown={handleNotesKeyDown}
            rows={2}
            style={{
              width: '100%',
              border: '1px solid #93c5fd',
              borderRadius: 4,
              padding: '4px 6px',
              fontSize: 12,
              fontFamily: 'inherit',
              resize: 'vertical',
              outline: 'none',
            }}
          />
        ) : (
          <div
            onClick={startEditingNotes}
            title={bol.notes || 'Click to add a note'}
            style={{
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              color: bol.notes ? '#374151' : '#d1d5db',
              fontSize: 12,
              cursor: 'text',
              minHeight: 16,
            }}
          >
            {bol.notes || '+ note'}
          </div>
        )}
      </td>

      {/* actions: routine zone + exception zone, fixed-size slots so every row is the same width */}
      <td style={{ ...TD, textAlign: 'center', borderLeft: '1px solid #f3f4f6' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-start', gap: 8 }}>
          {/* routine zone: Approve, Flag/Unflag, SID, Refresh BOL */}
          <div style={{ display: 'grid', gridTemplateColumns: '80px 26px 42px 42px', gap: 4, alignItems: 'center' }}>
            {/* Approve */}
            <button
              onClick={onApprove}
              disabled={isApproving || !approveEligible}
              title={
                !approveEligible
                  ? (isFlagged ? 'Unflag this record before approving' : 'No BOL number yet — create the Prophecy load first')
                  : 'Approve this record'
              }
              style={{
                background: isApproving ? '#d1fae5' : '#2D6A4F',
                color: isApproving ? '#065f46' : '#fff',
                border: 'none',
                borderRadius: 4,
                padding: '4px 0',
                width: '100%',
                fontSize: 12,
                fontWeight: 600,
                opacity: isApproving ? 0.7 : (approveEligible ? 1 : 0.5),
                cursor: (isApproving || !approveEligible) ? 'not-allowed' : 'pointer',
              }}
            >
              {isApproving ? '…' : '✓ Approve'}
            </button>
            {/* flag/unflag swaps in the same slot */}
            {isFlagged ? (
              <button
                onClick={onUnflag}
                disabled={isUnflagging}
                title={bol.flag_reason ? `Flagged: ${bol.flag_reason}` : 'Remove flag and return to pending'}
                style={{
                  ...ICON_BTN,
                  color: '#6b7280',
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
                style={{ ...ICON_BTN, color: '#92400e', borderColor: '#fcd34d', background: '#fff7ed' }}
              >
                ⚑
              </button>
            )}
            {/* export/check BOL -- only for pending Corp records */}
            {bol.needs_sid_export && bol.manifest && !bol.is_third_party ? (
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
            ) : (
              <div style={PLACEHOLDER} />
            )}
            {bol.needs_sid_export && bol.manifest && !bol.is_third_party ? (
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
            ) : (
              <div style={PLACEHOLDER} />
            )}
          </div>

          {/* Divider between routine and exception-handling actions */}
          <div style={{ width: 1, alignSelf: 'stretch', background: '#e5e7eb' }} />

          {/* exception zone: 3P | Retry+3P | Do Not Pay, mutually exclusive except the unresolved-invoice-only case */}
          <div style={{ width: isUnresolvedInvoiceOnly ? 88 : 44 }}>
            {isThirdPartyEligible(bol) ? (
              isUnresolvedInvoiceOnly ? (
                <div style={{ display: 'flex', gap: 4 }}>
                  <button
                    onClick={onRetryMatch}
                    disabled={isRetryingMatch}
                    title="Check Technique again (90-day window) for a trip matching this invoice's job name"
                    style={{
                      background: isRetryingMatch ? '#e5e7eb' : '#f9fafb',
                      color: '#374151',
                      border: '1px solid #d1d5db',
                      borderRadius: 4,
                      padding: '4px 0',
                      flex: 1,
                      fontSize: 11,
                      fontWeight: 600,
                      cursor: isRetryingMatch ? 'not-allowed' : 'pointer',
                      opacity: isRetryingMatch ? 0.7 : 1,
                    }}
                  >
                    {isRetryingMatch ? '…' : '🔍'}
                  </button>
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
                      flex: 1,
                      fontSize: 11,
                      fontWeight: 700,
                      cursor: isMarkingThirdParty ? 'not-allowed' : 'pointer',
                      opacity: isMarkingThirdParty ? 0.6 : 1,
                      letterSpacing: '0.02em',
                    }}
                  >
                    {isMarkingThirdParty ? '…' : '3P'}
                  </button>
                </div>
              ) : (
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
              )
            ) : bol.technique_trip == null && bol.invoice_number ? (
              <button
                onClick={() => onDoNotPay && onDoNotPay(bol.id, true)}
                disabled={isMarkingDoNotPay}
                title="Do Not Pay — approves this record into its sender's Approved batch, shows DO NOT PAY instead of an amount"
                style={{ background: 'none', border: 'none', padding: 0, fontSize: 11, color: '#9ca3af', cursor: isMarkingDoNotPay ? 'not-allowed' : 'pointer', textDecoration: 'underline', width: '100%' }}
              >
                {isMarkingDoNotPay ? '…' : 'Do Not Pay'}
              </button>
            ) : (
              <div style={PLACEHOLDER} />
            )}
          </div>

          {/* Compare (ambiguous trip) or Acknowledge (single-manifest mismatch), mutually exclusive */}
          <div style={{ width: 44 }}>
            {bol.is_ambiguous_trip && !bol.bol_number && !bol.is_third_party ? (
              <button
                onClick={() => onCompareOpen && onCompareOpen(bol.id)}
                title="Compare this trip's manifests and reassign the invoice if it's on the wrong one"
                style={{
                  background: '#fef3c7',
                  color: '#92400e',
                  border: '1px solid #fcd34d',
                  borderRadius: 4,
                  padding: '4px 0',
                  width: '100%',
                  fontSize: 11,
                  fontWeight: 700,
                  cursor: 'pointer',
                }}
              >
                ⚖ Compare
              </button>
            ) : isMismatchAcknowledgeEligible(bol) ? (
              <button
                onClick={onAcknowledgeMismatch}
                disabled={isAcknowledgingMismatch}
                title="Confirm this weight/pallet/piece mismatch is expected — clears the ~UNVERIFIED badge"
                style={{
                  background: isAcknowledgingMismatch ? '#e5e7eb' : '#f9fafb',
                  color: '#374151',
                  border: '1px solid #d1d5db',
                  borderRadius: 4,
                  padding: '4px 0',
                  width: '100%',
                  fontSize: 11,
                  fontWeight: 600,
                  cursor: isAcknowledgingMismatch ? 'not-allowed' : 'pointer',
                  opacity: isAcknowledgingMismatch ? 0.7 : 1,
                }}
              >
                {isAcknowledgingMismatch ? '…' : '✓ Acknowledge'}
              </button>
            ) : (
              <div style={PLACEHOLDER} />
            )}
          </div>
        </div>
      </td>
    </tr>
  );
}
