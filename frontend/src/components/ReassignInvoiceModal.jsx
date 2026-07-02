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
  width: 480,
  maxWidth: '95vw',
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

const LABEL = { fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 5, display: 'block' };

const INPUT_STYLE = {
  width: '100%',
  border: '1px solid #d1d5db',
  borderRadius: 5,
  padding: '7px 10px',
  fontSize: 13,
  outline: 'none',
  boxSizing: 'border-box',
};

const BTN_BASE = {
  borderRadius: 5,
  padding: '6px 14px',
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
  border: 'none',
};

export default function ReassignInvoiceModal({ bol, submitting, onClose, onReassign, onIgnore }) {
  const [target, setTarget] = useState('');
  const [preview, setPreview] = useState(null);   // null | { target_found, target_trip, target_invoice_number, target_amount, has_conflict }
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [conflictAction, setConflictAction] = useState(null);  // 'merge' | 'replace' | null

  useEffect(() => {
    setTarget('');
    setPreview(null);
    setPreviewError('');
    setConflictAction(null);
  }, [bol?.id]);

  if (!bol) return null;

  async function handlePreview() {
    if (!target.trim()) return;
    setPreviewing(true);
    setPreview(null);
    setPreviewError('');
    setConflictAction(null);
    try {
      const res = await fetch(`/api/bols/${bol.id}/reassign-invoice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: target.trim(), action: 'preview' }),
      });
      const data = await res.json();
      if (!res.ok) {
        setPreviewError(data.detail || 'Request failed');
      } else {
        setPreview(data);
      }
    } catch {
      setPreviewError('Network error');
    } finally {
      setPreviewing(false);
    }
  }

  async function handleMove() {
    if (!preview?.target_found) return;
    const action = preview.has_conflict ? (conflictAction || 'merge') : 'replace';
    await onReassign(bol.id, target.trim(), action);
  }

  const readyToMove = preview?.target_found && (!preview.has_conflict || conflictAction != null);
  const amountStr = v => v != null ? `$${parseFloat(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '—';

  return (
    <div style={OVERLAY} onClick={onClose}>
      <div style={CARD} onClick={e => e.stopPropagation()}>
        <h3 style={{ margin: '0 0 14px', fontSize: 15, fontWeight: 700, color: '#111827' }}>
          Reassign Invoice — {bol.invoice_number}
        </h3>

        <div style={INFO_BOX}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
            <span><strong>Invoice:</strong> {bol.invoice_number}</span>
            {(bol.invoice_number || '').split(',').map(z => z.trim()).filter(Boolean).map(z => (
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
          <div><strong>Amount:</strong> {amountStr(bol.amount)}</div>
          {bol.inv_job_number && <div><strong>Job name:</strong> {bol.inv_job_number}</div>}
          {bol.technique_trip && <div><strong>Current trip:</strong> {bol.technique_trip}</div>}
          {bol.match_strategy && <div><strong>Matched via:</strong> {bol.match_strategy}</div>}
        </div>

        <label style={LABEL}>Reassign to trip #, BOL #, or manifest #</label>
        <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
          <input
            style={INPUT_STYLE}
            placeholder="e.g. TEC_T_0110707, 146415, or 110707"
            value={target}
            onChange={e => { setTarget(e.target.value); setPreview(null); setPreviewError(''); setConflictAction(null); }}
            onKeyDown={e => e.key === 'Enter' && handlePreview()}
          />
          <button
            style={{ ...BTN_BASE, background: '#1e40af', color: '#fff', whiteSpace: 'nowrap', opacity: !target.trim() || previewing ? 0.6 : 1 }}
            onClick={handlePreview}
            disabled={!target.trim() || previewing}
          >
            {previewing ? '…' : 'Preview'}
          </button>
        </div>

        {previewError && (
          <div style={{ color: '#dc2626', fontSize: 13, marginBottom: 12 }}>
            ❌ {previewError}
          </div>
        )}

        {preview && !preview.target_found && (
          <div style={{ color: '#dc2626', fontSize: 13, marginBottom: 12 }}>
            ❌ No record found matching "{target}"
          </div>
        )}

        {preview?.target_found && !preview.has_conflict && (
          <div style={{ background: '#f0fdf4', border: '1px solid #86efac', borderRadius: 6, padding: '10px 14px', fontSize: 13, marginBottom: 14 }}>
            ✓ <strong>{preview.target_trip || '(no trip)'}</strong> — no existing invoice. Ready to move.
          </div>
        )}

        {preview?.target_found && preview.has_conflict && (
          <div style={{ background: '#fffbeb', border: '1px solid #fcd34d', borderRadius: 6, padding: '10px 14px', fontSize: 13, marginBottom: 14 }}>
            <div style={{ fontWeight: 600, color: '#92400e', marginBottom: 8 }}>
              ⚠ {preview.target_trip} already has {preview.target_invoice_number} ({amountStr(preview.target_amount)})
            </div>
            <div style={{ color: '#374151', marginBottom: 8 }}>Choose an action:</div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                style={{ ...BTN_BASE, background: conflictAction === 'merge' ? '#1e40af' : '#e5e7eb', color: conflictAction === 'merge' ? '#fff' : '#374151', fontSize: 12 }}
                onClick={() => setConflictAction('merge')}
              >
                Merge amounts
              </button>
              <button
                style={{ ...BTN_BASE, background: conflictAction === 'replace' ? '#dc2626' : '#e5e7eb', color: conflictAction === 'replace' ? '#fff' : '#374151', fontSize: 12 }}
                onClick={() => setConflictAction('replace')}
              >
                Replace
              </button>
            </div>
            {conflictAction === 'merge' && (
              <div style={{ marginTop: 8, color: '#6b7280', fontSize: 12 }}>
                Merge: existing {amountStr(preview.target_amount)} + new {amountStr(bol.amount)} = {amountStr((parseFloat(preview.target_amount || 0) + parseFloat(bol.amount || 0)).toFixed(2))}
              </div>
            )}
            {conflictAction === 'replace' && (
              <div style={{ marginTop: 8, color: '#6b7280', fontSize: 12 }}>
                Replace: {preview.target_invoice_number} will be removed and replaced with {bol.invoice_number}
              </div>
            )}
          </div>
        )}

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 8 }}>
          <button
            style={{ ...BTN_BASE, background: 'none', border: 'none', color: '#6b7280', fontSize: 12, padding: '6px 0', cursor: 'pointer' }}
            onClick={() => onIgnore(bol.id)}
            disabled={submitting}
            title="Mark this invoice record as ignored — stays in log but excluded from exports"
          >
            Ignore this invoice
          </button>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              style={{ ...BTN_BASE, background: '#f3f4f6', color: '#374151', border: '1px solid #d1d5db' }}
              onClick={onClose}
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              style={{ ...BTN_BASE, background: readyToMove ? '#1e40af' : '#9ca3af', color: '#fff', opacity: submitting ? 0.7 : 1 }}
              onClick={handleMove}
              disabled={!readyToMove || submitting}
            >
              {submitting ? 'Moving…' : 'Move Invoice'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
