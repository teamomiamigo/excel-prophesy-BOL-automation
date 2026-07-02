import { useState, useEffect } from 'react';
import SummaryBar from './components/SummaryBar.jsx';
import BOLTable from './components/BOLTable.jsx';
import ThirdPartySection from './components/ThirdPartySection.jsx';
import ApprovedSection from './components/ApprovedSection.jsx';
import FlagModal from './components/FlagModal.jsx';
import ReassignInvoiceModal from './components/ReassignInvoiceModal.jsx';
import LogSection from './components/LogSection.jsx';
import BulkActionToolbar from './components/BulkActionToolbar.jsx';

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
  const [sidLoading, setSidLoading] = useState(false);
  const [invoiceUploading, setInvoiceUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [uploadResults, setUploadResults] = useState(null); // { matched, unmatched, errors, conflicts }
  const [pollResults, setPollResults] = useState(null);     // same shape, from poll-folder
  const [unapprovingId, setUnapprovingId] = useState(null);
  const [unflaggingId, setUnflaggingId] = useState(null);
  const [markingThirdPartyId, setMarkingThirdPartyId] = useState(null);
  const [unmarkingThirdPartyId, setUnmarkingThirdPartyId] = useState(null);
  const [reassignTargetId, setReassignTargetId] = useState(null);
  const [reassignSubmitting, setReassignSubmitting] = useState(false);
  const [ignoringId, setIgnoringId] = useState(null);
  const [activeTab, setActiveTab] = useState('dashboard'); // 'dashboard' | 'log'
  const [pullLoading, setPullLoading] = useState(false);
  const [pollFolderLoading, setPollFolderLoading] = useState(false);
  const [filterText, setFilterText] = useState('');
  const [sort, setSort] = useState({ column: null, direction: 'default' });
  const [uploadSender, setUploadSender] = useState('');
  const [uploadDate, setUploadDate] = useState('');
  const [uploadTime, setUploadTime] = useState('');
  const [showSenderFields, setShowSenderFields] = useState(false);
  const [sidExportedThisSession, setSidExportedThisSession] = useState(false);
  const [exportingSidId, setExportingSidId] = useState(null);
  const [checkingBolId, setCheckingBolId] = useState(null);
  const [refreshLoading, setRefreshLoading] = useState(false);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [bulkActionLoading, setBulkActionLoading] = useState(false);
  const [bulkFlagOpen, setBulkFlagOpen] = useState(false);
  const [bulkResults, setBulkResults] = useState(null); // { action, succeeded, total, skipped }

  const thirdPartyBols     = pendingBols.filter(b => b.is_third_party);
  const visiblePendingBols = pendingBols.filter(b => !b.is_third_party);

  const summary = {
    manifestOnly:  visiblePendingBols.filter(b => b.technique_trip != null && b.amount == null).length,
    invoiceOnly:   visiblePendingBols.filter(b => b.technique_trip == null).length,
    readyToReview: visiblePendingBols.filter(b => b.technique_trip != null && b.amount != null).length,
    approvedToday: approvedBols.length,
  };

  // -------------------------------------------------------------------------
  // Selection (issue #32 — multi-select bulk actions)
  // -------------------------------------------------------------------------

  function toggleSelect(id) {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function toggleSelectAll(visibleIds) {
    setSelectedIds(prev => {
      const allSelected = visibleIds.length > 0 && visibleIds.every(id => prev.has(id));
      const next = new Set(prev);
      if (allSelected) {
        visibleIds.forEach(id => next.delete(id));
      } else {
        visibleIds.forEach(id => next.add(id));
      }
      return next;
    });
  }

  function clearSelection() {
    setSelectedIds(new Set());
  }

  // -------------------------------------------------------------------------
  // Sorting (issue #33 — sortable columns)
  // -------------------------------------------------------------------------

  function handleSort(column) {
    setSort(prev => {
      if (prev.column !== column) return { column, direction: 'asc' };
      if (prev.direction === 'asc') return { column, direction: 'desc' };
      return { column: null, direction: 'default' };
    });
  }

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

  async function handleExportRecordToProphecy(recordId) {
    setExportingSidId(recordId);
    try {
      const res = await fetch(`/api/bols/${recordId}/export-prophecy-sid`, { method: 'POST' });
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
      setSuccessMessage('Prophecy SID file downloaded for this record — import it into Prophecy to create the load number.');
      await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setExportingSidId(null);
    }
  }

  async function handleCheckBol(recordId) {
    setCheckingBolId(recordId);
    try {
      const res = await fetch(`/api/bols/${recordId}/refresh-bol`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Check BOL failed (${res.status})`);
      setSuccessMessage(data.message || (data.updated ? 'BOL found.' : 'No BOL yet.'));
      if (data.updated) await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setCheckingBolId(null);
    }
  }

  async function handleRefresh() {
    setRefreshLoading(true);
    try {
      await Promise.all([fetchPending(), fetchApproved()]);
    } catch (err) {
      setError(err.message);
    } finally {
      setRefreshLoading(false);
    }
  }

  // -------------------------------------------------------------------------
  // Bulk actions (issue #32) — fire the same per-record endpoints used by the
  // individual action buttons, once per eligible selected record, then a
  // single refresh. Eligibility mirrors each action's per-row button
  // condition in BOLRow.jsx.
  // -------------------------------------------------------------------------

  function selectedRecords() {
    return visiblePendingBols.filter(b => selectedIds.has(b.id));
  }

  async function handleBulkApprove() {
    const targets = selectedRecords(); // no eligibility restriction — matches per-row Approve
    if (!targets.length) return;
    setBulkActionLoading(true);
    try {
      const results = await Promise.allSettled(
        targets.map(b => fetch(`/api/bols/${b.id}/approve`, { method: 'POST' }))
      );
      const succeeded = results.filter(r => r.status === 'fulfilled' && r.value.ok).length;
      setBulkResults({ action: 'approved', succeeded, total: targets.length, skipped: 0 });
      await Promise.all([fetchPending(), fetchApproved()]);
      clearSelection();
    } catch (err) {
      setError(err.message);
    } finally {
      setBulkActionLoading(false);
    }
  }

  function openBulkFlag() {
    if (!selectedRecords().length) return;
    setBulkFlagOpen(true);
  }

  async function handleBulkFlagSubmit(reason) {
    const all = selectedRecords();
    const eligible = all.filter(b => b.status !== 'flagged');
    const skipped = all.length - eligible.length;
    setFlagSubmitting(true);
    try {
      const results = await Promise.allSettled(
        eligible.map(b => fetch(`/api/bols/${b.id}/flag`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason }),
        }))
      );
      const succeeded = results.filter(r => r.status === 'fulfilled' && r.value.ok).length;
      setBulkFlagOpen(false);
      setBulkResults({ action: 'flagged', succeeded, total: all.length, skipped });
      await Promise.all([fetchPending(), fetchApproved()]);
      clearSelection();
    } catch (err) {
      setError(err.message);
    } finally {
      setFlagSubmitting(false);
    }
  }

  async function handleBulkMarkThirdParty() {
    const all = selectedRecords();
    const eligible = all.filter(b => !b.amount && !b.bol_number && !b.is_third_party);
    const skipped = all.length - eligible.length;
    if (!all.length) return;
    setBulkActionLoading(true);
    try {
      const results = await Promise.allSettled(
        eligible.map(b => fetch(`/api/bols/${b.id}/mark-third-party`, { method: 'POST' }))
      );
      const succeeded = results.filter(r => r.status === 'fulfilled' && r.value.ok).length;
      setBulkResults({ action: 'marked third-party', succeeded, total: all.length, skipped });
      await fetchPending();
      clearSelection();
    } catch (err) {
      setError(err.message);
    } finally {
      setBulkActionLoading(false);
    }
  }

  async function handleBulkIgnore() {
    const all = selectedRecords();
    const eligible = all.filter(b => b.technique_trip == null && b.invoice_number && !b.is_ignored);
    const skipped = all.length - eligible.length;
    if (!all.length) return;
    setBulkActionLoading(true);
    try {
      const results = await Promise.allSettled(
        eligible.map(b => fetch(`/api/bols/${b.id}/ignore`, { method: 'POST' }))
      );
      const succeeded = results.filter(r => r.status === 'fulfilled' && r.value.ok).length;
      setBulkResults({ action: 'ignored', succeeded, total: all.length, skipped });
      await fetchPending();
      clearSelection();
    } catch (err) {
      setError(err.message);
    } finally {
      setBulkActionLoading(false);
    }
  }

  async function handleBulkExportSid() {
    const all = selectedRecords();
    const eligible = all.filter(b => b.needs_sid_export && b.manifest && !b.is_third_party && !b.is_ignored);
    const skipped = all.length - eligible.length;
    if (!all.length) return;
    setBulkActionLoading(true);
    try {
      // Sequential, not Promise.all — reduces (doesn't fully eliminate) the chance
      // the browser blocks/prompts on multiple simultaneous downloads. Reuses the
      // exact per-record handler from #35 unchanged, so each file is identical to
      // what clicking that record's own SID button would produce.
      for (const b of eligible) {
        await handleExportRecordToProphecy(b.id);
      }
      setBulkResults({ action: 'SID-exported', succeeded: eligible.length, total: all.length, skipped });
      clearSelection();
    } catch (err) {
      setError(err.message);
    } finally {
      setBulkActionLoading(false);
    }
  }

  async function handleInvoiceUpload(e) {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    e.target.value = '';
    setInvoiceUploading(true);
    setUploadResults(null);
    const matched = [], unmatched = [], errors = [], conflicts = [];
    for (let i = 0; i < files.length; i++) {
      setUploadProgress(`${i + 1} of ${files.length}`);
      const form = new FormData();
      form.append('file', files[i]);
      if (uploadSender) form.append('invoice_sender', uploadSender);
      if (uploadDate) form.append('invoice_date', uploadDate);
      if (uploadTime) form.append('invoice_time', uploadTime);
      try {
        const res = await fetch('/api/invoices/upload', { method: 'POST', body: form });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          errors.push({ name: files[i].name, msg: data.detail || `HTTP ${res.status}` });
        } else if (data.matched) {
          matched.push({ name: files[i].name, invoice: data.invoice_number, trip: data.matched_trip, strategy: data.match_strategy });
          if (data.conflict) conflicts.push(data.conflict);
        } else {
          unmatched.push({ name: files[i].name, invoice: data.invoice_number, jobName: data.job_name, note: data.message });
        }
      } catch (err) {
        errors.push({ name: files[i].name, msg: err.message });
      }
    }
    setInvoiceUploading(false);
    setUploadProgress(null);
    setUploadResults({ matched, unmatched, errors, conflicts });
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

  async function handleMarkThirdParty(recordId) {
    setMarkingThirdPartyId(recordId);
    try {
      const res = await fetch(`/api/bols/${recordId}/mark-third-party`, { method: 'POST' });
      if (!res.ok) throw new Error(`Mark third-party failed (${res.status})`);
      await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setMarkingThirdPartyId(null);
    }
  }

  async function handleUnmarkThirdParty(recordId) {
    setUnmarkingThirdPartyId(recordId);
    try {
      const res = await fetch(`/api/bols/${recordId}/unmark-third-party`, { method: 'POST' });
      if (!res.ok) throw new Error(`Unmark third-party failed (${res.status})`);
      await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setUnmarkingThirdPartyId(null);
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
    setPollResults(null);
    try {
      const res = await fetch('/api/invoices/poll-folder', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Folder poll failed (${res.status})`);

      if ((data.found || 0) === 0) {
        setSuccessMessage(data.message || 'No new invoice files found.');
      } else {
        const matched = [], unmatched = [], errors = [], conflicts = [];
        for (const r of (data.processed || [])) {
          if (r.error) {
            errors.push({ name: r.filename || r.invoice_number || '?', msg: r.error });
          } else if (r.matched && r.match_strategy !== 'invoice_only') {
            matched.push({ name: r.invoice_number, invoice: r.invoice_number, trip: r.matched_trip, strategy: r.match_strategy });
            if (r.conflict) conflicts.push(r.conflict);
          } else {
            unmatched.push({ name: r.invoice_number, invoice: r.invoice_number, jobName: r.job_name, note: r.message });
          }
        }
        setPollResults({ matched, unmatched, errors, conflicts });
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

  async function handleReassignInvoice(recordId, target, action) {
    setReassignSubmitting(true);
    try {
      const res = await fetch(`/api/bols/${recordId}/reassign-invoice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, action }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Reassign failed (${res.status})`);
      }
      setReassignTargetId(null);
      await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setReassignSubmitting(false);
    }
  }

  async function handleIgnore(recordId, shouldIgnore) {
    setIgnoringId(recordId);
    try {
      const route = shouldIgnore ? 'ignore' : 'unignore';
      const res = await fetch(`/api/bols/${recordId}/${route}`, { method: 'POST' });
      if (!res.ok) throw new Error(`${route} failed (${res.status})`);
      setReassignTargetId(null);
      await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setIgnoringId(null);
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

        {/* Bulk action results */}
        {bulkResults && (
          <div style={{
            background: bulkResults.skipped > 0 ? '#fffbeb' : '#f0fdf4',
            border: `1px solid ${bulkResults.skipped > 0 ? '#fde68a' : '#bbf7d0'}`,
            borderRadius: 6,
            padding: '10px 16px',
            marginBottom: 16,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            color: bulkResults.skipped > 0 ? '#92400e' : '#166534',
          }}>
            <span>
              {bulkResults.succeeded} of {bulkResults.total} records {bulkResults.action}
              {bulkResults.skipped > 0 && ` — ${bulkResults.skipped} skipped (not eligible)`}
            </span>
            <button
              onClick={() => setBulkResults(null)}
              style={{ background: 'none', border: 'none', color: 'inherit', fontSize: 16, padding: '0 4px' }}
            >×</button>
          </div>
        )}

        {activeTab === 'dashboard' && (
          <>
            <SummaryBar
              manifestOnly={summary.manifestOnly}
              invoiceOnly={summary.invoiceOnly}
              readyToReview={summary.readyToReview}
              approvedToday={summary.approvedToday}
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
              <span>{summary.manifestOnly + summary.invoiceOnly + summary.readyToReview + thirdPartyBols.length + summary.approvedToday} records loaded</span>
              <span>·</span>
              <span>{summary.readyToReview} ready &nbsp;·&nbsp; {summary.manifestOnly} manifest only &nbsp;·&nbsp; {summary.invoiceOnly} invoice only &nbsp;·&nbsp; {summary.approvedToday} approved</span>
              <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
                <button
                  onClick={handleRefresh}
                  disabled={refreshLoading}
                  title="Refresh pending and approved records from this dashboard's own data (no live Technique pull)"
                  style={{
                    background: refreshLoading ? '#e5e7eb' : '#f9fafb',
                    color: refreshLoading ? '#9ca3af' : '#374151',
                    border: '1px solid #d1d5db',
                    borderRadius: 5,
                    padding: '4px 12px',
                    fontWeight: 600,
                    fontSize: 12,
                    cursor: refreshLoading ? 'not-allowed' : 'pointer',
                  }}
                >
                  {refreshLoading ? 'Refreshing…' : '⟳ Refresh'}
                </button>
                <button
                  onClick={handlePull}
                  disabled={pullLoading}
                  title="Pull latest manifests from Technique"
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
                  {pullLoading ? 'Pulling…' : '↻ Pull Manifests'}
                </button>
                <button
                  onClick={handlePollFolder}
                  disabled={pollFolderLoading}
                  title="Scan invoice folder for new ALG CSVs and process them"
                  style={{
                    background: pollFolderLoading ? '#e5e7eb' : '#f0fdf4',
                    color: pollFolderLoading ? '#9ca3af' : '#2D6A4F',
                    border: '1px solid #bbf7d0',
                    borderRadius: 5,
                    padding: '4px 12px',
                    fontWeight: 600,
                    fontSize: 12,
                    cursor: pollFolderLoading ? 'not-allowed' : 'pointer',
                  }}
                >
                  {pollFolderLoading ? 'Scanning…' : '⤓ Pull Invoices'}
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
                <button
                  type="button"
                  onClick={() => setShowSenderFields(v => !v)}
                  title="Add sender info for manual uploads"
                  style={{
                    background: showSenderFields ? '#fef3c7' : '#fff',
                    color: '#92400e',
                    border: '1px solid #fde68a',
                    borderRadius: 5,
                    padding: '4px 10px',
                    fontSize: 12,
                    cursor: 'pointer',
                  }}
                >
                  {showSenderFields ? '▲ Sender' : '▼ Sender'}
                </button>
              </span>
            </div>
            {showSenderFields && (
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '8px 14px', background: '#fffbeb', border: '1px solid #fde68a', borderRadius: 6, marginBottom: 12, fontSize: 12 }}>
                <span style={{ fontWeight: 600, color: '#92400e', whiteSpace: 'nowrap' }}>Sender info for manual upload:</span>
                <input
                  type="text"
                  value={uploadSender}
                  onChange={e => setUploadSender(e.target.value)}
                  placeholder="Sender name (e.g. Tania)"
                  style={{ border: '1px solid #d1d5db', borderRadius: 4, padding: '4px 8px', fontSize: 12, width: 160 }}
                />
                <input
                  type="date"
                  value={uploadDate}
                  onChange={e => setUploadDate(e.target.value)}
                  style={{ border: '1px solid #d1d5db', borderRadius: 4, padding: '4px 8px', fontSize: 12 }}
                />
                <input
                  type="time"
                  value={uploadTime}
                  onChange={e => setUploadTime(e.target.value)}
                  style={{ border: '1px solid #d1d5db', borderRadius: 4, padding: '4px 8px', fontSize: 12 }}
                />
                <span style={{ color: '#9ca3af' }}>Optional — leave blank if unknown</span>
              </div>
            )}

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
                {(uploadResults.conflicts || []).length > 0 && (
                  <div style={{ background: '#fffbeb', borderTop: '2px solid #fcd34d', padding: '8px 12px' }}>
                    <div style={{ fontWeight: 700, color: '#92400e', marginBottom: 6, fontSize: 12 }}>
                      ⚠ {uploadResults.conflicts.length} invoice conflict{uploadResults.conflicts.length > 1 ? 's' : ''} — auto-merged, review recommended
                    </div>
                    {uploadResults.conflicts.map((c, i) => (
                      <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'center', fontSize: 12, color: '#374151', marginBottom: 4 }}>
                        <span style={{ fontWeight: 600, color: '#92400e' }}>{c.invoice_number}</span>
                        <span>auto-merged with {c.matched_trip} (already had {c.existing_invoice})</span>
                        <button
                          onClick={() => setReassignTargetId(c.record_id)}
                          style={{ marginLeft: 'auto', background: '#fff7ed', border: '1px solid #fed7aa', borderRadius: 4, padding: '2px 8px', fontSize: 11, fontWeight: 600, color: '#c2410c', cursor: 'pointer' }}
                        >
                          Reassign
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {pollResults && (pollResults.matched.length + pollResults.unmatched.length + pollResults.errors.length > 0) && (
              <div style={{ marginBottom: 16, border: '1px solid #e5e7eb', borderRadius: 6, overflow: 'hidden', fontSize: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 12px', background: '#f9fafb', borderBottom: '1px solid #e5e7eb' }}>
                  <span style={{ fontWeight: 600, color: '#374151' }}>
                    Pull Invoices Results — {pollResults.matched.length} matched &nbsp;·&nbsp; {pollResults.unmatched.length} unmatched &nbsp;·&nbsp; {pollResults.errors.length} errors
                  </span>
                  <button onClick={() => setPollResults(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#9ca3af', fontSize: 14, lineHeight: 1 }}>✕</button>
                </div>
                {pollResults.matched.map((r, i) => (
                  <div key={i} style={{ display: 'flex', gap: 12, padding: '5px 12px', background: '#f0fdf4', borderBottom: '1px solid #dcfce7', alignItems: 'center' }}>
                    <span style={{ color: '#16a34a', fontWeight: 700, minWidth: 14 }}>✓</span>
                    <span style={{ fontWeight: 600, color: '#166534', minWidth: 90 }}>{r.invoice}</span>
                    <span style={{ marginLeft: 'auto', color: '#6b7280' }}>→ {r.trip} <span style={{ background: '#dcfce7', color: '#166534', borderRadius: 3, padding: '1px 5px', fontSize: 11 }}>{r.strategy}</span></span>
                  </div>
                ))}
                {pollResults.unmatched.map((r, i) => (
                  <div key={i} style={{ display: 'flex', gap: 12, padding: '5px 12px', background: '#fffbeb', borderBottom: '1px solid #fef3c7', alignItems: 'center' }}>
                    <span style={{ color: '#d97706', fontWeight: 700, minWidth: 14 }}>—</span>
                    <span style={{ fontWeight: 600, color: '#92400e', minWidth: 90 }}>{r.invoice}</span>
                    <span style={{ marginLeft: 'auto', color: '#6b7280', fontStyle: 'italic' }}>{r.note}</span>
                  </div>
                ))}
                {pollResults.errors.map((r, i) => (
                  <div key={i} style={{ display: 'flex', gap: 12, padding: '5px 12px', background: '#fef2f2', borderBottom: '1px solid #fecaca', alignItems: 'center' }}>
                    <span style={{ color: '#dc2626', fontWeight: 700, minWidth: 14 }}>✕</span>
                    <span style={{ color: '#374151' }}>{r.name}</span>
                    <span style={{ marginLeft: 'auto', color: '#991b1b' }}>{r.msg}</span>
                  </div>
                ))}
                {(pollResults.conflicts || []).length > 0 && (
                  <div style={{ background: '#fffbeb', borderTop: '2px solid #fcd34d', padding: '8px 12px' }}>
                    <div style={{ fontWeight: 700, color: '#92400e', marginBottom: 6, fontSize: 12 }}>
                      ⚠ {pollResults.conflicts.length} invoice conflict{pollResults.conflicts.length > 1 ? 's' : ''} — auto-merged, review recommended
                    </div>
                    {pollResults.conflicts.map((c, i) => (
                      <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'center', fontSize: 12, color: '#374151', marginBottom: 4 }}>
                        <span style={{ fontWeight: 600, color: '#92400e' }}>{c.invoice_number}</span>
                        <span>auto-merged with {c.matched_trip} (already had {c.existing_invoice})</span>
                        <button
                          onClick={() => setReassignTargetId(c.record_id)}
                          style={{ marginLeft: 'auto', background: '#fff7ed', border: '1px solid #fed7aa', borderRadius: 4, padding: '2px 8px', fontSize: 11, fontWeight: 600, color: '#c2410c', cursor: 'pointer' }}
                        >
                          Reassign
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            <BOLTable
              bols={visiblePendingBols}
              loading={loadingPending && pendingBols.length === 0}
              approvingId={approvingId}
              unflaggingId={unflaggingId}
              markingThirdPartyId={markingThirdPartyId}
              ignoringId={ignoringId}
              exportingSidId={exportingSidId}
              checkingBolId={checkingBolId}
              filterText={filterText}
              onFilterChange={setFilterText}
              selectedIds={selectedIds}
              onToggleSelect={toggleSelect}
              onToggleSelectAll={toggleSelectAll}
              sort={sort}
              onSort={handleSort}
              onApprove={handleApprove}
              onFlagOpen={setFlagTarget}
              onUnflag={handleUnflag}
              onNotesUpdate={handleNotesUpdate}
              onMarkThirdParty={handleMarkThirdParty}
              onReassignOpen={id => setReassignTargetId(id)}
              onIgnore={handleIgnore}
              onExportSid={handleExportRecordToProphecy}
              onCheckBol={handleCheckBol}
            />

            <ThirdPartySection
              bols={thirdPartyBols}
              approvingId={approvingId}
              unmarkingThirdPartyId={unmarkingThirdPartyId}
              onApprove={handleApprove}
              onUnmark={handleUnmarkThirdParty}
            />

            <ApprovedSection
              approvedBols={approvedBols}
              loading={loadingApproved && approvedBols.length === 0}
              sendLoading={sendLoading}
              sidLoading={sidLoading}
              sidExportedThisSession={sidExportedThisSession}
              unapprovingId={unapprovingId}
              onUnapprove={handleUnapprove}
              onConfirmSend={handleSendToAccounting}
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

      {bulkFlagOpen && (
        <FlagModal
          count={selectedRecords().length}
          submitting={flagSubmitting}
          onClose={() => setBulkFlagOpen(false)}
          onSubmit={handleBulkFlagSubmit}
        />
      )}

      {reassignTargetId && (
        <ReassignInvoiceModal
          bol={pendingBols.find(b => b.id === reassignTargetId) || null}
          submitting={reassignSubmitting}
          onClose={() => setReassignTargetId(null)}
          onReassign={handleReassignInvoice}
          onIgnore={handleIgnore}
        />
      )}

      <BulkActionToolbar
        count={selectedIds.size}
        loading={bulkActionLoading}
        onApprove={handleBulkApprove}
        onFlag={openBulkFlag}
        onMarkThirdParty={handleBulkMarkThirdParty}
        onIgnore={handleBulkIgnore}
        onExportSid={handleBulkExportSid}
        onClear={clearSelection}
      />
    </div>
  );
}
