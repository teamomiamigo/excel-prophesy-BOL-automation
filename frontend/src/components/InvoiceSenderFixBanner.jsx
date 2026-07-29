import { useState } from 'react';

// shown under a batch whose folder name failed to parse into a date; disappears on its own
// once the parent refetch picks up a real invoice_sent_at (BOLTable.jsx's needsCorrection)
export default function InvoiceSenderFixBanner({ rawSender, recordCount, submitting, onSubmit }) {
  const [value, setValue] = useState('');

  function handleSubmit(e) {
    e.preventDefault();
    if (!value.trim()) return;
    onSubmit(rawSender, value.trim());
  }

  return (
    <div style={{ background: '#fffbeb', borderTop: '1px solid #fde68a', borderBottom: '1px solid #fde68a', padding: '8px 10px', fontSize: 12 }}>
      <div style={{ color: '#92400e', marginBottom: 6 }}>
        ⚠ This batch's folder name couldn't be parsed as a date ("{rawSender}") — {recordCount} record{recordCount !== 1 ? 's' : ''} won't sort by recency until corrected.
      </div>
      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8 }}>
        <input
          value={value}
          onChange={e => setValue(e.target.value)}
          placeholder='e.g. "Tania 7-22-2026 436PM"'
          style={{ flex: 1, border: '1px solid #fcd34d', borderRadius: 4, padding: '4px 8px', fontSize: 12 }}
        />
        <button
          type="submit"
          disabled={submitting || !value.trim()}
          style={{
            background: submitting ? '#fef3c7' : '#f59e0b',
            color: submitting ? '#92400e' : '#fff',
            border: 'none',
            borderRadius: 4,
            padding: '4px 12px',
            fontSize: 12,
            fontWeight: 600,
            cursor: submitting || !value.trim() ? 'not-allowed' : 'pointer',
            opacity: !value.trim() ? 0.6 : 1,
          }}
        >
          {submitting ? 'Saving…' : 'Fix'}
        </button>
      </form>
    </div>
  );
}
