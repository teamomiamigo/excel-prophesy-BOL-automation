import { useState, useEffect } from 'react';

const OVERLAY = {
  position: 'fixed', inset: 0,
  background: 'rgba(0,0,0,0.45)',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  zIndex: 1000,
};

const CARD = {
  background: '#fff',
  borderRadius: 10,
  padding: '24px 28px',
  width: 860,
  maxWidth: '95vw',
  maxHeight: '85vh',
  overflowY: 'auto',
  boxShadow: '0 8px 32px rgba(0,0,0,0.18)',
};

const INFO_BOX = {
  background: '#f3f4f6',
  borderRadius: 6,
  padding: '10px 14px',
  marginBottom: 18,
  fontSize: 13,
  lineHeight: 1.6,
};

const BTN_BASE = {
  borderRadius: 5,
  padding: '5px 10px',
  fontSize: 12,
  fontWeight: 600,
  cursor: 'pointer',
  border: 'none',
};

const TH = {
  textAlign: 'left',
  fontSize: 11,
  fontWeight: 700,
  color: '#6b7280',
  padding: '6px 8px',
  borderBottom: '2px solid #e5e7eb',
  textTransform: 'uppercase',
  letterSpacing: '0.03em',
  whiteSpace: 'nowrap',
};

const TH_R = { ...TH, textAlign: 'right' };

const TD = {
  fontSize: 13,
  padding: '8px 8px',
  borderBottom: '1px solid #f3f4f6',
  whiteSpace: 'nowrap',
};

const TD_R = { ...TD, textAlign: 'right' };

// A candidate this modal is not offering an "Assign here" action for — either
// it's the manifest the invoice already landed on, there's no invoice on this
// trip at all yet, or Katie's already told us this leg is third-party.
function isAssignable(candidate, referenceId) {
  return candidate.id !== referenceId && !candidate.is_third_party;
}

// A candidate that can be dismissed as a bad/duplicate manifest — never the one
// actually holding the invoice, and never one that already has its own real
// invoice attached (that's a genuinely separate load, not junk data).
function isDismissable(candidate, referenceId) {
  return candidate.id !== referenceId && !candidate.invoice_number;
}

export default function CompareManifestsModal({ bol, submitting, onClose, onReassign, onDismiss }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState('');
  const [conflictRowId, setConflictRowId] = useState(null);
  const [conflictAction, setConflictAction] = useState(null); // 'merge' | 'replace' | null
  const [dismissingId, setDismissingId] = useState(null);

  useEffect(() => {
    setData(null);
    setFetchError('');
    setConflictRowId(null);
    setConflictAction(null);
    if (!bol?.id) return;
    setLoading(true);
    fetch(`/api/bols/${bol.id}/trip-manifests`)
      .then(async res => {
        const body = await res.json();
        if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
        setData(body);
      })
      .catch(err => setFetchError(err.message || 'Network error'))
      .finally(() => setLoading(false));
  }, [bol?.id]);

  if (!bol) return null;

  const amountStr = v => v != null ? `$${parseFloat(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '—';
  const numStr = v => v != null ? parseInt(v).toLocaleString('en-US') : '—';

  function startAssign(candidate) {
    if (candidate.invoice_number) {
      setConflictRowId(candidate.id);
      setConflictAction(null);
    } else {
      onReassign(data.reference_id, candidate.manifest, 'replace');
    }
  }

  async function handleDelete(candidate) {
    setDismissingId(candidate.id);
    try {
      const ok = await onDismiss(candidate.id);
      if (ok) {
        setData(prev => prev && { ...prev, candidates: prev.candidates.filter(c => c.id !== candidate.id) });
      }
    } finally {
      setDismissingId(null);
    }
  }

  function confirmConflict(candidate) {
    if (!conflictAction) return;
    onReassign(data.reference_id, candidate.manifest, conflictAction);
  }

  return (
    <div style={OVERLAY} onClick={onClose}>
      <div style={CARD} onClick={e => e.stopPropagation()}>
        <h3 style={{ margin: '0 0 14px', fontSize: 15, fontWeight: 700, color: '#111827' }}>
          Compare Manifests — Trip {bol.technique_trip}
        </h3>

        {loading && <div style={{ fontSize: 13, color: '#6b7280', marginBottom: 14 }}>Loading…</div>}
        {fetchError && (
          <div style={{ color: '#dc2626', fontSize: 13, marginBottom: 14 }}>❌ {fetchError}</div>
        )}

        {data && (
          <>
            <div style={INFO_BOX}>
              {data.reference_id ? (
                <>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
                    <span><strong>Invoice:</strong> {data.invoice_number}</span>
                    {(data.invoice_number || '').split(',').map(z => z.trim()).filter(Boolean).map(z => (
                      <a
                        key={z}
                        href={`/api/invoices/${z}/file`}
                        target="_blank"
                        rel="noreferrer"
                        title={`Open invoice ${z}`}
                        style={{ fontSize: 11, color: '#1e40af', textDecoration: 'none', background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 4, padding: '1px 7px', fontWeight: 600, whiteSpace: 'nowrap' }}
                      >
                        📄 {z}
                      </a>
                    ))}
                  </div>
                  <div><strong>Amount:</strong> {amountStr(data.amount)}</div>
                  <div><strong>ALG quantities:</strong> {numStr(data.alg_weight)} lbs / {numStr(data.alg_pallets)} pal / {numStr(data.alg_pcs)} pcs</div>
                  {data.inv_job_number && <div><strong>Job name:</strong> {data.inv_job_number}</div>}
                  {data.invoice_email_sender && <div><strong>From:</strong> {data.invoice_email_sender}</div>}
                </>
              ) : (
                <div style={{ color: '#6b7280' }}>
                  No invoice has arrived for this trip yet — showing manifest quantities only.
                </div>
              )}
            </div>

            <table style={{ width: '100%', borderCollapse: 'collapse', marginBottom: 14 }}>
              <thead>
                <tr>
                  <th style={TH}>Manifest</th>
                  <th style={TH_R}>Weight</th>
                  <th style={TH_R}>Pallets</th>
                  <th style={TH_R}>Pcs</th>
                  <th style={TH_R}>ΔWgt</th>
                  <th style={TH_R}>ΔPal</th>
                  <th style={TH_R}>ΔPcs</th>
                  <th style={TH_R}>Score</th>
                  <th style={TH}>Status</th>
                  <th style={TH}></th>
                </tr>
              </thead>
              <tbody>
                {data.candidates.map(c => {
                  const isReference = c.id === data.reference_id;
                  const assignable = isAssignable(c, data.reference_id) && !!data.reference_id;
                  const dismissable = isDismissable(c, data.reference_id);
                  // Diffs match the main dashboard's convention: ALG (invoice) minus our
                  // own Technique quantities. Only meaningful once an invoice has arrived.
                  const diffStr = (algVal, techVal) => {
                    if (algVal == null || techVal == null) return '—';
                    const d = Math.round(parseFloat(algVal) - parseFloat(techVal));
                    return d > 0 ? `+${d.toLocaleString('en-US')}` : d.toLocaleString('en-US');
                  };
                  return (
                    <tr key={c.id} style={{ background: isReference ? '#eff6ff' : undefined, opacity: c.is_third_party ? 0.55 : 1 }}>
                      <td style={TD}>
                        {c.manifest || <span style={{ color: '#d1d5db' }}>—</span>}
                        {isReference && (
                          <span style={{ marginLeft: 6, fontSize: 10, background: '#dbeafe', color: '#1e40af', borderRadius: 3, padding: '1px 5px', fontWeight: 700, letterSpacing: '0.02em' }}>
                            CURRENT
                          </span>
                        )}
                      </td>
                      <td style={TD_R}>{numStr(c.technique_weight)}</td>
                      <td style={TD_R}>{numStr(c.technique_pallets)}</td>
                      <td style={TD_R}>{numStr(c.technique_pcs)}</td>
                      <td style={{ ...TD_R, color: '#6b7280' }}>{diffStr(data.alg_weight, c.technique_weight)}</td>
                      <td style={{ ...TD_R, color: '#6b7280' }}>{diffStr(data.alg_pallets, c.technique_pallets)}</td>
                      <td style={{ ...TD_R, color: '#6b7280' }}>{diffStr(data.alg_pcs, c.technique_pcs)}</td>
                      <td style={TD_R}>
                        {c.score != null ? c.score.toFixed(2) : '—'}
                        {c.is_best_fit && !isReference && (
                          <span style={{ marginLeft: 6, fontSize: 10, background: '#dcfce7', color: '#166534', borderRadius: 3, padding: '1px 5px', fontWeight: 700, letterSpacing: '0.02em' }}>
                            BEST FIT
                          </span>
                        )}
                      </td>
                      <td style={{ ...TD, color: '#6b7280', fontSize: 12 }}>
                        {c.bol_number ? `BOL ${c.bol_number}` : 'no BOL yet'}
                        {c.is_third_party ? ' · 3P' : ''}
                        {c.invoice_number && !isReference ? ` · has ${c.invoice_number}` : ''}
                      </td>
                      <td style={TD}>
                        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                          {assignable && (
                            conflictRowId === c.id ? (
                              <>
                                <button
                                  style={{ ...BTN_BASE, background: conflictAction === 'merge' ? '#1e40af' : '#e5e7eb', color: conflictAction === 'merge' ? '#fff' : '#374151' }}
                                  onClick={() => setConflictAction('merge')}
                                >
                                  Merge
                                </button>
                                <button
                                  style={{ ...BTN_BASE, background: conflictAction === 'replace' ? '#dc2626' : '#e5e7eb', color: conflictAction === 'replace' ? '#fff' : '#374151' }}
                                  onClick={() => setConflictAction('replace')}
                                >
                                  Replace
                                </button>
                                <button
                                  style={{ ...BTN_BASE, background: conflictAction ? '#1e40af' : '#9ca3af', color: '#fff', opacity: submitting ? 0.7 : 1 }}
                                  onClick={() => confirmConflict(c)}
                                  disabled={!conflictAction || submitting}
                                >
                                  {submitting ? '…' : 'Confirm'}
                                </button>
                              </>
                            ) : (
                              <button
                                style={{ ...BTN_BASE, background: '#eff6ff', color: '#1e40af', border: '1px solid #bfdbfe' }}
                                onClick={() => startAssign(c)}
                                disabled={submitting}
                                title={c.invoice_number ? `Already holds ${c.invoice_number} — choose merge or replace` : 'Move this invoice to this manifest'}
                              >
                                Assign here
                              </button>
                            )
                          )}
                          {dismissable && conflictRowId !== c.id && (
                            <button
                              style={{ ...BTN_BASE, background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', opacity: dismissingId === c.id ? 0.7 : 1 }}
                              onClick={() => handleDelete(c)}
                              disabled={dismissingId === c.id}
                              title="Dismiss this manifest as bad/duplicate data — hides it here and from the pending queue"
                            >
                              {dismissingId === c.id ? '…' : '🗑 Delete'}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button
            style={{ ...BTN_BASE, background: '#f3f4f6', color: '#374151', border: '1px solid #d1d5db', padding: '6px 14px' }}
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
