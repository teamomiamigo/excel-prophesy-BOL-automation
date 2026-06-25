import { useState, useEffect } from 'react';

export default function FlagModal({ bol, submitting, onClose, onSubmit }) {
  const [reason, setReason] = useState('');

  // Reset reason when modal opens on a different BOL
  useEffect(() => {
    setReason('');
  }, [bol?.id]);

  function handleSubmit(e) {
    e.preventDefault();
    if (reason.trim().length < 3) return;
    onSubmit(reason.trim());
  }

  return (
    // Full-screen overlay
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.45)',
        zIndex: 1000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      {/* Modal card — stop clicks from bubbling to overlay */}
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: '#fff',
          borderRadius: 10,
          boxShadow: '0 20px 60px rgba(0,0,0,0.25)',
          padding: 28,
          width: 460,
          maxWidth: '90vw',
        }}
      >
        <div style={{ marginBottom: 16 }}>
          <h3 style={{ fontSize: 16, fontWeight: 700, color: '#111827', marginBottom: 4 }}>
            Flag Record
          </h3>
          <p style={{ fontSize: 13, color: '#6b7280' }}>
            Invoice {bol.invoice_number}
            {bol.amount != null && ` — $${parseFloat(bol.amount).toLocaleString('en-US', { minimumFractionDigits: 2 })}`}
          </p>
        </div>

        <form onSubmit={handleSubmit}>
          <label style={{ display: 'block', fontSize: 13, fontWeight: 600, color: '#374151', marginBottom: 6 }}>
            Reason for flagging
          </label>
          <textarea
            value={reason}
            onChange={e => setReason(e.target.value)}
            placeholder="Describe the issue (e.g., weight discrepancy, cost % out of range…)"
            required
            minLength={3}
            rows={4}
            style={{
              width: '100%',
              border: '1px solid #d1d5db',
              borderRadius: 6,
              padding: '10px 12px',
              fontSize: 13,
              resize: 'vertical',
              outline: 'none',
            }}
            autoFocus
          />

          <div style={{ display: 'flex', gap: 10, marginTop: 18, justifyContent: 'flex-end' }}>
            <button
              type="button"
              onClick={onClose}
              style={{
                background: '#fff',
                color: '#6b7280',
                border: '1px solid #d1d5db',
                borderRadius: 6,
                padding: '8px 18px',
                fontSize: 13,
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || reason.trim().length < 3}
              style={{
                background: reason.trim().length >= 3 ? '#b45309' : '#d1d5db',
                color: '#fff',
                border: 'none',
                borderRadius: 6,
                padding: '8px 18px',
                fontSize: 13,
                fontWeight: 600,
                cursor: submitting || reason.trim().length < 3 ? 'not-allowed' : 'pointer',
              }}
            >
              {submitting ? 'Flagging…' : '⚑ Flag Record'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
