import { useState } from 'react';

function fmtMoney(val) {
  if (val == null) return '—';
  return `$${parseFloat(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function amountCell(r) {
  return r.is_do_not_pay ? 'DO NOT PAY' : fmtMoney(r.amount);
}

export default function EmailComposeModal({ records, senderLabel, onClose, onMarkSent }) {
  const [toField, setToField] = useState('');
  const [subjectField, setSubjectField] = useState(`SG360 BOL Invoices — ${senderLabel}`);
  const [copied, setCopied] = useState(false);
  const [markingLoading, setMarkingLoading] = useState(false);
  const [markedSent, setMarkedSent] = useState(false);
  const [error, setError] = useState(null);

  function buildHtmlTable() {
    const rows = records.map(r => `
      <tr>
        <td style="padding:6px 12px;border:1px solid #d1d5db;">${r.bol_number ?? '—'}</td>
        <td style="padding:6px 12px;border:1px solid #d1d5db;">${r.invoice_number || '—'}</td>
        <td style="padding:6px 12px;border:1px solid #d1d5db;">${r.invoice_email_sender || '—'}</td>
        <td style="padding:6px 12px;border:1px solid #d1d5db;text-align:right;">${amountCell(r)}</td>
      </tr>`).join('');
    return `<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;">
      <thead>
        <tr style="background:#374151;color:#fff;">
          <th style="padding:8px 12px;border:1px solid #4b5563;">BOL</th>
          <th style="padding:8px 12px;border:1px solid #4b5563;">Invoice #</th>
          <th style="padding:8px 12px;border:1px solid #4b5563;">Sender</th>
          <th style="padding:8px 12px;border:1px solid #4b5563;text-align:right;">Amount</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
  }

  function buildPlainTable() {
    const pad = (s, n) => String(s).padEnd(n);
    const padL = (s, n) => String(s).padStart(n);
    const header = `${pad('BOL', 10)} | ${pad('Invoice #', 10)} | ${pad('Sender', 32)} | ${pad('Amount', 11)}`;
    const sep    = `${'-'.repeat(10)}-+-${'-'.repeat(10)}-+-${'-'.repeat(32)}-+-${'-'.repeat(11)}`;
    const rows = records.map(r =>
      `${pad(r.bol_number ?? '—', 10)} | ${pad(r.invoice_number || '—', 10)} | ${pad(r.invoice_email_sender || '—', 32)} | ${padL(amountCell(r), 11)}`
    ).join('\n');
    return [header, sep, rows].join('\n');
  }

  function handleDownloadInvoices() {
    // senderLabel is the invoice_email_sender this batch is grouped by — the
    // same key the backend merged/stored a combined PDF under at upload time
    // (see App.jsx's uploadInvoiceFiles). 'No Sender' means these records
    // never carried a real sender label (e.g. a manual single-invoice
    // upload), so there's no precomputed batch key to ask for.
    if (senderLabel && senderLabel !== 'No Sender') {
      window.open(`/api/invoices/batch-pdf?sender=${encodeURIComponent(senderLabel)}`, '_blank');
      return;
    }
    const zNumbers = records
      .flatMap(r => (r.invoice_number || '').split(',').map(z => z.trim()).filter(Boolean))
      .filter((z, i, arr) => arr.indexOf(z) === i); // dedupe
    if (zNumbers.length === 0) return;
    window.open(`/api/export/invoice-pdfs?invoice_numbers=${zNumbers.join(',')}`, '_blank');
  }

  async function handleCopyTable() {
    try {
      const htmlBlob = new Blob([buildHtmlTable()], { type: 'text/html' });
      const textBlob = new Blob([buildPlainTable()], { type: 'text/plain' });
      await navigator.clipboard.write([new ClipboardItem({ 'text/html': htmlBlob, 'text/plain': textBlob })]);
    } catch {
      await navigator.clipboard.writeText(buildPlainTable());
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2500);
  }

  function handleOpenOutlook() {
    const to = toField.trim();
    const subject = encodeURIComponent(subjectField);
    const body = encodeURIComponent(buildPlainTable());
    window.location.href = `mailto:${to}?subject=${subject}&body=${body}`;
    // mailto links can't carry an attachment — there's no way around that
    // browser/email-client limitation — so trigger the merged invoice PDF
    // download at the same time, ready to drag into the draft this just opened.
    handleDownloadInvoices();
  }

  async function handleMarkSent() {
    setMarkingLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/bols/mark-accounting-sent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ record_ids: records.map(r => r.id) }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      setMarkedSent(true);
      setTimeout(() => {
        onMarkSent(records.map(r => r.id));
      }, 1200);
    } catch (err) {
      setError(err.message);
    } finally {
      setMarkingLoading(false);
    }
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0,
        background: 'rgba(0,0,0,0.45)',
        zIndex: 1000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: '#fff',
          borderRadius: 10,
          padding: '24px 28px',
          width: 720,
          maxWidth: '95vw',
          maxHeight: '90vh',
          overflowY: 'auto',
          boxShadow: '0 20px 40px rgba(0,0,0,0.18)',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16 }}>
          <h3 style={{ fontSize: 16, fontWeight: 700, color: '#111827', margin: 0 }}>
            Send to Accounting — {records.length} record{records.length !== 1 ? 's' : ''}
          </h3>
          <button onClick={onClose} style={{ background: 'none', border: 'none', fontSize: 20, cursor: 'pointer', color: '#9ca3af', lineHeight: 1 }}>×</button>
        </div>

        <div style={{ display: 'grid', gap: 8, marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <label style={{ fontSize: 12, fontWeight: 600, color: '#6b7280', width: 56, flexShrink: 0 }}>To:</label>
            <input
              value={toField}
              onChange={e => setToField(e.target.value)}
              placeholder="mary@sg360.com, katie@sg360.com"
              style={{ flex: 1, padding: '6px 10px', border: '1px solid #d1d5db', borderRadius: 5, fontSize: 13 }}
            />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <label style={{ fontSize: 12, fontWeight: 600, color: '#6b7280', width: 56, flexShrink: 0 }}>Subject:</label>
            <input
              value={subjectField}
              onChange={e => setSubjectField(e.target.value)}
              style={{ flex: 1, padding: '6px 10px', border: '1px solid #d1d5db', borderRadius: 5, fontSize: 13 }}
            />
          </div>
        </div>

        <div style={{ overflowX: 'auto', borderRadius: 6, border: '1px solid #e5e7eb', marginBottom: 16 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ background: '#374151', color: '#fff' }}>
                <th style={{ padding: '7px 12px', textAlign: 'left', fontWeight: 600 }}>BOL</th>
                <th style={{ padding: '7px 12px', textAlign: 'left', fontWeight: 600 }}>Invoice #</th>
                <th style={{ padding: '7px 12px', textAlign: 'left', fontWeight: 600 }}>Sender</th>
                <th style={{ padding: '7px 12px', textAlign: 'right', fontWeight: 600 }}>Amount</th>
              </tr>
            </thead>
            <tbody>
              {records.map(r => (
                <tr key={r.id} style={{ borderBottom: '1px solid #f3f4f6' }}>
                  <td style={{ padding: '6px 12px' }}>
                    {r.bol_number ?? <span style={{ color: '#9ca3af' }}>—</span>}
                  </td>
                  <td style={{ padding: '6px 12px' }}>{r.invoice_number || '—'}</td>
                  <td style={{ padding: '6px 12px', color: '#6b7280', fontSize: 12 }}>{r.invoice_email_sender || '—'}</td>
                  <td style={{ padding: '6px 12px', textAlign: 'right', fontWeight: 600, color: r.is_do_not_pay ? '#dc2626' : undefined }}>{amountCell(r)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {error && (
          <div style={{ color: '#dc2626', fontSize: 13, marginBottom: 12, padding: '8px 12px', background: '#fef2f2', borderRadius: 5 }}>
            {error}
          </div>
        )}

        {markedSent ? (
          <div style={{
            padding: '14px 16px',
            background: '#f0fdf4',
            border: '1px solid #bbf7d0',
            borderRadius: 6,
            color: '#166534',
            fontWeight: 600,
            fontSize: 13,
            textAlign: 'center',
          }}>
            ✓ Marked as sent — these records will move to the Log.
          </div>
        ) : (
          <div style={{ display: 'flex', gap: 10, justifyContent: 'space-between', alignItems: 'center' }}>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                onClick={handleCopyTable}
                title="Copy table as HTML — paste directly into Outlook"
                style={{
                  background: '#f9fafb',
                  color: '#374151',
                  border: '1px solid #d1d5db',
                  borderRadius: 5,
                  padding: '7px 14px',
                  fontSize: 13,
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                {copied ? '✓ Copied!' : 'Copy Table'}
              </button>
              <button
                onClick={handleOpenOutlook}
                title="Open Outlook draft with plain-text table"
                style={{
                  background: '#f9fafb',
                  color: '#374151',
                  border: '1px solid #d1d5db',
                  borderRadius: 5,
                  padding: '7px 14px',
                  fontSize: 13,
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                Open in Outlook
              </button>
              <button
                onClick={handleDownloadInvoices}
                title="Download all invoice PDFs for this batch merged into one file — attach to your email"
                style={{
                  background: '#f9fafb',
                  color: '#374151',
                  border: '1px solid #d1d5db',
                  borderRadius: 5,
                  padding: '7px 14px',
                  fontSize: 13,
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                Download Invoices
              </button>
            </div>
            <button
              onClick={handleMarkSent}
              disabled={markingLoading}
              style={{
                background: markingLoading ? '#9ca3af' : '#2D6A4F',
                color: '#fff',
                border: 'none',
                borderRadius: 5,
                padding: '7px 20px',
                fontSize: 13,
                fontWeight: 600,
                cursor: markingLoading ? 'not-allowed' : 'pointer',
              }}
            >
              {markingLoading ? 'Saving…' : 'Mark as Sent ✓'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
