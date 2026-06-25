import { useState, useEffect } from 'react';
import SummaryBar from './components/SummaryBar.jsx';
import BOLTable from './components/BOLTable.jsx';
import ApprovedSection from './components/ApprovedSection.jsx';
import FlagModal from './components/FlagModal.jsx';
import LogSection from './components/LogSection.jsx';

// When Module 2 ships: extract fetch helpers to src/api/bolsApi.js
// and move this state/logic to src/pages/BolReconciliation.jsx

export default function App() {
  const [pendingBols, setPendingBols] = useState([]);
  const [approvedBols, setApprovedBols] = useState([]);
  const [loadingPending, setLoadingPending] = useState(true);
  const [loadingApproved, setLoadingApproved] = useState(true);
  const [error, setError] = useState(null);
  const [successMessage, setSuccessMessage] = useState(null);

  const [approvingId, setApprovingId] = useState(null);
  const [flagTarget, setFlagTarget] = useState(null);
  const [flagSubmitting, setFlagSubmitting] = useState(false);

  const [sendLoading, setSendLoading] = useState(false);
  const [sendConfirmPending, setSendConfirmPending] = useState(false);
  const [sidLoading, setSidLoading] = useState(false);
  const [invoiceUploading, setInvoiceUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [uploadResults, setUploadResults] = useState(null); // { matched, unmatched, errors }
  const [unapprovingId, setUnapprovingId] = useState(null);
  const [unflaggingId, setUnflaggingId] = useState(null);
  const [activeTab, setActiveTab] = useState('dashboard'); // 'dashboard' | 'log'
  const [pullLoading, setPullLoading] = useState(false);
  const [pollFolderLoading, setPollFolderLoading] = useState(false);
  const [filterText, setFilterText] = useState('');
  const [sidExportedThisSession, setSidExportedThisSession] = useState(false);

  const summary = {
    manifestOnly:  pendingBols.filter(b => b.technique_trip != null && b.amount == null).length,
    invoiceOnly:   pendingBols.filter(b => b.technique_trip == null).length,
    readyToReview: pendingBols.filter(b => b.technique_trip != null && b.amount != null).length,
    approvedToday: approvedBols.length,
  };

  // -------------------------------------------------------------------------
  // Fetch helpers
  // -------------------------------------------------------------------------

  async function fetchPending() {
    setLoadingPending(true);
    try {
      const res = await fetch('/api/bols');
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      setPendingBols(await res.json());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoadingPending(false);
    }
  }

  async function fetchApproved() {
    setLoadingApproved(true);
    try {
      const res = await fetch('/api/bols/approved');
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      setApprovedBols(await res.json());
      setSidExportedThisSession(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoadingApproved(false);
    }
  }

  useEffect(() => {
    fetchPending();
    fetchApproved();
  }, []);

  // -------------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------------

  async function handleApprove(recordId) {
    setApprovingId(recordId);
    try {
      const res = await fetch(`/api/bols/${recordId}/approve`, { method: 'POST' });
      if (!res.ok) throw new Error(`Approve failed (${res.status})`);
      await Promise.all([fetchPending(), fetchApproved()]);
    } catch (err) {
      setError(err.message);
    } finally {
      setApprovingId(null);
    }
  }

  async function handleFlagSubmit(reason) {
    if (!flagTarget) return;
    setFlagSubmitting(true);
    try {
      const res = await fetch(`/api/bols/${flagTarget.id}/flag`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      if (!res.ok) throw new Error(`Flag failed (${res.status})`);
      setFlagTarget(null);
      await Promise.all([fetchPending(), fetchApproved()]);
    } catch (err) {
      setError(err.message);
      setFlagTarget(null);
    } finally {
      setFlagSubmitting(false);
    }
  }

  async function handleUnapprove(recordId) {
    setUnapprovingId(recordId);
    try {
      const res = await fetch(`/api/bols/${recordId}/unapprove`, { method: 'POST' });
      if (!res.ok) throw new Error(`Revert failed (${res.status})`);
      await Promise.all([fetchPending(), fetchApproved()]);
    } catch (err) {
      setError(err.message);
    } finally {
      setUnapprovingId(null);
    }
  }

  async function handleExportProphecy() {
    setSidLoading(true);
    try {
      const res = await fetch('/api/export/prophecy-sid');
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `SID export failed (${res.status})`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="([^"]+)"/);
      a.href = url;
      a.download = match ? match[1] : 'SG360_Prophecy_SID.csv';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setSuccessMessage('Prophecy SID file downloaded — import it into Prophecy to create load numbers.');
      setSidExportedThisSession(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setSidLoading(false);
    }
  }

  async function handleInvoiceUpload(e) {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    e.target.value = '';
    setInvoiceUploading(true);
    setUploadResults(null);
    const matched = [], unmatched = [], errors = [];
    for (let i = 0; i < files.length; i++) {
      setUploadProgress(`${i + 1} of ${files.length}`);
      const form = new FormData();
      form.append('file', files[i]);
      try {
        const res = await fetch('/api/invoices/upload', { method: 'POST', body: form });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          errors.push({ name: files[i].name, msg: data.detail || `HTTP ${res.status}` });
        } else if (data.matched) {
          matched.push({ name: files[i].name, invoice: data.invoice_number, trip: data.matched_trip, strategy: data.match_strategy });
        } else {
          unmatched.push({ name: files[i].name, invoice: data.invoice_number, jobName: data.job_name, note: data.message });
        }
      } catch (err) {
        errors.push({ name: files[i].name, msg: err.message });
      }
    }
    setInvoiceUploading(false);
    setUploadProgress(null);
    setUploadResults({ matched, unmatched, errors });
    await Promise.all([fetchPending(), fetchApproved()]);
  }

  async function handleUnflag(recordId) {
    setUnflaggingId(recordId);
    try {
      const res = await fetch(`/api/bols/${recordId}/unflag`, { method: 'POST' });
      if (!res.ok) throw new Error(`Unflag failed (${res.status})`);
      await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setUnflaggingId(null);
    }
  }

  async function handleNotesUpdate(recordId, notes) {
    try {
      const res = await fetch(`/api/bols/${recordId}/notes`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ notes }),
      });
      if (!res.ok) throw new Error(`Notes save failed (${res.status})`);
      // Update local state in-place rather than refetching all records
      setPendingBols(prev => prev.map(b => b.id === recordId ? { ...b, notes } : b));
    } catch (err) {
      setError(err.message);
    }
  }

  async function handlePollFolder() {
    setPollFolderLoading(true);
    try {
      const res = await fetch('/api/invoices/poll-folder', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Folder poll failed (${res.status})`);
      setSuccessMessage(data.message || 'Invoice folder checked.');
      if ((data.found || 0) > 0) {
        await Promise.all([fetchPending(), fetchApproved()]);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setPollFolderLoading(false);
    }
  }

  async function handlePull() {
    setPullLoading(true);
    try {
      const res = await fetch('/api/admin/pull', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Pull failed (${res.status})`);
      setSuccessMessage(data.message || 'Technique data refreshed.');
      await Promise.all([fetchPending(), fetchApproved()]);
    } catch (err) {
      setError(err.message);
    } finally {
      setPullLoading(false);
    }
  }

  async function handleSendToAccounting() {
    setSendConfirmPending(false);
    setSendLoading(true);
    try {
      const res = await fetch('/api/export', { method: 'POST' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Export failed (${res.status})`);
      }
      const data = await res.json();
      setSuccessMessage(data.message);
    } catch (err) {
      setError(err.message);
    } finally {
      setSendLoading(false);
    }
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div style={{ minHeight: '100vh', background: '#f4f5f7' }}>
      {/* Header */}
      <header style={{
        background: '#1A1A1A',
        color: '#fff',
        padding: '0 24px',
        height: 56,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{
            background: '#2D6A4F',
            color: '#fff',
            fontWeight: 700,
            fontSize: 13,
            padding: '3px 8px',
            borderRadius: 4,
            letterSpacing: '0.05em',
          }}>SG360</span>
          <span style={{ fontWeight: 600, fontSize: 15 }}>BOL Reconciliation</span>
        </div>
        <span style={{ fontSize: 12, color: '#9ca3af' }}>
          {new Date().toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })}
        </span>
      </header>

      {/* Tab bar */}
      <div style={{ background: '#fff', borderBottom: '1px solid #e5e7eb', padding: '0 24px', display: 'flex', gap: 0 }}>
        {[
          { key: 'dashboard', label: 'Dashboard' },
          { key: 'log',       label: 'Log' },
        ].map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            style={{
              background: 'none',
              border: 'none',
              borderBottom: activeTab === tab.key ? '2px solid #2D6A4F' : '2px solid transparent',
              color: activeTab === tab.key ? '#2D6A4F' : '#6b7280',
              fontWeight: activeTab === tab.key ? 700 : 400,
              fontSize: 13,
              padding: '10px 18px',
              cursor: 'pointer',
              marginBottom: -1,
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <main style={{ padding: '20px 24px', maxWidth: 1800, margin: '0 auto' }}>
        {/* Error banner */}
        {error && (
          <div style={{
            background: '#fef2f2',
            border: '1px solid #fecaca',
            borderRadius: 6,
            padding: '10px 16px',
            marginBottom: 16,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            color: '#991b1b',
          }}>
            <span>{error}</span>
            <button
              onClick={() => setError(null)}
              style={{ background: 'none', border: 'none', color: '#991b1b', fontSize: 16, padding: '0 4px' }}
            >×</button>
          </div>
        )}

        {/* Success banner */}
        {successMessage && (
          <div style={{
            background: '#f0fdf4',
            border: '1px solid #bbf7d0',
            borderRadius: 6,
            padding: '10px 16px',
            marginBottom: 16,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            color: '#166534',
          }}>
            <span>{successMessage}</span>
            <button
              onClick={() => setSuccessMessage(null)}
              style={{ background: 'none', border: 'none', color: '#166534', fontSize: 16, padding: '0 4px' }}
            >×</button>
          </div>
        )}

        {activeTab === 'dashboard' && (
          <>
            <SummaryBar
              pending={summary.pending}
              approved={summary.approved}
              flagged={summary.flagged}
            />

            {/* Date / context banner + invoice upload */}
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: 16,
              padding: '8px 14px',
              marginBottom: 16,
              background: '#fff',
              border: '1px solid #e5e7eb',
              borderRadius: 6,
              fontSize: 12,
              color: '#6b7280',
            }}>
              <span style={{ fontWeight: 600, color: '#374151' }}>
                {new Date().toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })}
              </span>
              <span>·</span>
              <span>{summary.pending + summary.flagged + summary.approved} records loaded</span>
              <span>·</span>
              <span>{summary.pending} pending &nbsp;·&nbsp; {summary.flagged} flagged &nbsp;·&nbsp; {summary.approved} approved</span>
              <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
                <button
                  onClick={handlePull}
                  disabled={pullLoading}
                  title="Pull latest manifests from Technique (last 10 days)"
                  style={{
                    background: pullLoading ? '#e5e7eb' : '#f9fafb',
                    color: pullLoading ? '#9ca3af' : '#374151',
                    border: '1px solid #d1d5db',
                    borderRadius: 5,
                    padding: '4px 12px',
                    fontWeight: 600,
                    fontSize: 12,
                    cursor: pullLoading ? 'not-allowed' : 'pointer',
                  }}
                >
                  {pullLoading ? 'Pulling…' : '↻ Refresh Manifests'}
                </button>
                <button
                  onClick={handleCheckEmail}
                  disabled={emailLoading}
                  title="Check O365 inbox for new ALG invoice emails from Tanya and process any CSV attachments"
                  style={{
                    background: emailLoading ? '#e5e7eb' : '#faf5ff',
                    color: emailLoading ? '#9ca3af' : '#7c3aed',
                    border: '1px solid #ddd6fe',
                    borderRadius: 5,
                    padding: '4px 12px',
                    fontWeight: 600,
                    fontSize: 12,
                    cursor: emailLoading ? 'not-allowed' : 'pointer',
                  }}
                >
                  {emailLoading ? 'Checking…' : '✉ Check Email'}
                </button>
                <label style={{
                  display: 'inline-block',
                  background: invoiceUploading ? '#e5e7eb' : '#f0f9ff',
                  color: invoiceUploading ? '#9ca3af' : '#0369a1',
                  border: '1px solid #bae6fd',
                  borderRadius: 5,
                  padding: '4px 12px',
                  fontWeight: 600,
                  fontSize: 12,
                  cursor: invoiceUploading ? 'not-allowed' : 'pointer',
                }}>
                  {invoiceUploading ? `Uploading ${uploadProgress}…` : 'Upload Invoices'}
                  <input
                    type="file"
                    accept=".csv"
                    multiple
                    style={{ display: 'none' }}
                    disabled={invoiceUploading}
                    onChange={handleInvoiceUpload}
                  />
                </label>
              </span>
            </div>

            {uploadResults && (uploadResults.matched.length + uploadResults.unmatched.length + uploadResults.errors.length > 0) && (
              <div style={{ marginBottom: 16, border: '1px solid #e5e7eb', borderRadius: 6, overflow: 'hidden', fontSize: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 12px', background: '#f9fafb', borderBottom: '1px solid #e5e7eb' }}>
                  <span style={{ fontWeight: 600, color: '#374151' }}>
                    Invoice Upload Results — {uploadResults.matched.length} matched &nbsp;·&nbsp; {uploadResults.unmatched.length} unmatched &nbsp;·&nbsp; {uploadResults.errors.length} errors
                  </span>
                  <button onClick={() => setUploadResults(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#9ca3af', fontSize: 14, lineHeight: 1 }}>✕</button>
                </div>
                {uploadResults.matched.map((r, i) => (
                  <div key={i} style={{ display: 'flex', gap: 12, padding: '5px 12px', background: '#f0fdf4', borderBottom: '1px solid #dcfce7', alignItems: 'center' }}>
                    <span style={{ color: '#16a34a', fontWeight: 700, minWidth: 14 }}>✓</span>
                    <span style={{ fontWeight: 600, color: '#166534', minWidth: 90 }}>{r.invoice}</span>
                    <span style={{ color: '#374151' }}>{r.name}</span>
                    <span style={{ marginLeft: 'auto', color: '#6b7280' }}>→ {r.trip} <span style={{ background: '#dcfce7', color: '#166534', borderRadius: 3, padding: '1px 5px', fontSize: 11 }}>{r.strategy}</span></span>
                  </div>
                ))}
                {uploadResults.unmatched.map((r, i) => (
                  <div key={i} style={{ display: 'flex', gap: 12, padding: '5px 12px', background: '#fffbeb', borderBottom: '1px solid #fef3c7', alignItems: 'center' }}>
                    <span style={{ color: '#d97706', fontWeight: 700, minWidth: 14 }}>—</span>
                    <span style={{ fontWeight: 600, color: '#92400e', minWidth: 90 }}>{r.invoice}</span>
                    <span style={{ color: '#374151' }}>{r.name}</span>
                    <span style={{ marginLeft: 'auto', color: '#6b7280', fontStyle: 'italic' }}>{r.note}</span>
                  </div>
                ))}
                {uploadResults.errors.map((r, i) => (
                  <div key={i} style={{ display: 'flex', gap: 12, padding: '5px 12px', background: '#fef2f2', borderBottom: '1px solid #fecaca', alignItems: 'center' }}>
                    <span style={{ color: '#dc2626', fontWeight: 700, minWidth: 14 }}>✕</span>
                    <span style={{ color: '#374151' }}>{r.name}</span>
                    <span style={{ marginLeft: 'auto', color: '#991b1b' }}>{r.msg}</span>
                  </div>
                ))}
              </div>
            )}

            <BOLTable
              bols={pendingBols}
              loading={loadingPending}
              approvingId={approvingId}
              unflaggingId={unflaggingId}
              onApprove={handleApprove}
              onFlagOpen={setFlagTarget}
              onUnflag={handleUnflag}
              onNotesUpdate={handleNotesUpdate}
            />

            <ApprovedSection
              approvedBols={approvedBols}
              loading={loadingApproved}
              sendLoading={sendLoading}
              sendConfirmPending={sendConfirmPending}
              sidLoading={sidLoading}
              unapprovingId={unapprovingId}
              onUnapprove={handleUnapprove}
              onSendToAccounting={() => setSendConfirmPending(true)}
              onConfirmSend={handleSendToAccounting}
              onCancelSend={() => setSendConfirmPending(false)}
              onExportProphecy={handleExportProphecy}
            />
          </>
        )}

        {activeTab === 'log' && <LogSection />}
      </main>

      {flagTarget && (
        <FlagModal
          bol={flagTarget}
          submitting={flagSubmitting}
          onClose={() => setFlagTarget(null)}
          onSubmit={handleFlagSubmit}
        />
      )}
    </div>
  );
}
