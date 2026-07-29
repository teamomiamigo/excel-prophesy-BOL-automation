import { useState, useEffect, useRef } from 'react';
import SummaryBar from './components/SummaryBar.jsx';
import BOLTable from './components/BOLTable.jsx';
import { isDoNotPayEligible, isThirdPartyEligible, isApproveEligible } from './components/BOLRow.jsx';
import ThirdPartySection from './components/ThirdPartySection.jsx';
import ApprovedSection from './components/ApprovedSection.jsx';
import FlagModal from './components/FlagModal.jsx';
import ReassignInvoiceModal from './components/ReassignInvoiceModal.jsx';
import CompareManifestsModal from './components/CompareManifestsModal.jsx';
import LogSection from './components/LogSection.jsx';
import BulkActionToolbar from './components/BulkActionToolbar.jsx';

// Module 2: extract fetch helpers to src/api/bolsApi.js, move this state to src/pages/BolReconciliation.jsx

// bounded-concurrency runner so a batch of retry-match searches doesn't hammer AWP-SQL-PROD
async function runWithConcurrency(items, limit, fn) {
  const queue = [...items];
  async function worker() {
    while (queue.length > 0) {
      const item = queue.shift();
      await fn(item);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
}

const AUTO_RETRY_CONCURRENCY = 3;

// a "timed_out" response means the search didn't finish, not that the trip was confirmed
// absent -- worth another attempt rather than recording it as a permanent miss
const AUTO_RETRY_MAX_ATTEMPTS = 3;

// patches the initial "unmatched" list with results from the automatic retry-match pass
// that runs right after upload/poll, so the summary doesn't keep showing a stale miss
function reconcileWithRetryResults(unmatched, unmatchedByRecordId, retryResults) {
  const stillUnmatched = [...unmatched];
  const newlyMatched = [];
  for (const [id, result] of retryResults) {
    const entry = unmatchedByRecordId.get(id);
    if (!entry) continue;
    if (result.matched) {
      const idx = stillUnmatched.indexOf(entry);
      if (idx !== -1) stillUnmatched.splice(idx, 1);
      newlyMatched.push({ ...entry, trip: result.trip, strategy: 'job_name', note: undefined });
    } else if (result.message) {
      entry.note = result.message;
    }
  }
  return { stillUnmatched, newlyMatched };
}

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

  const [sidLoading, setSidLoading] = useState(false);
  const [invoiceUploading, setInvoiceUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [uploadResults, setUploadResults] = useState(null); // { matched, unmatched, errors, conflicts }
  const [pollResults, setPollResults] = useState(null);     // same shape, from poll-folder
  const [unapprovingId, setUnapprovingId] = useState(null);
  const [unflaggingId, setUnflaggingId] = useState(null);
  const [markingThirdPartyId, setMarkingThirdPartyId] = useState(null);
  const [unmarkingThirdPartyId, setUnmarkingThirdPartyId] = useState(null);
  const [movingToLogLoading, setMovingToLogLoading] = useState(false);
  const [reassignTargetId, setReassignTargetId] = useState(null);
  const [reassignSubmitting, setReassignSubmitting] = useState(false);
  const [compareTargetId, setCompareTargetId] = useState(null);
  const [markingDoNotPayId, setMarkingDoNotPayId] = useState(null);
  const [activeTab, setActiveTab] = useState('dashboard'); // 'dashboard' | 'log'
  const [pollFolderLoading, setPollFolderLoading] = useState(false);
  const [filterText, setFilterText] = useState('');
  const [sort, setSort] = useState({ column: null, direction: 'default' });
  const [groupOrder, setGroupOrder] = useState('desc'); // 'desc' = newest sender-batch first (default); 'asc' = oldest first
  const [fixSenderSubmitting, setFixSenderSubmitting] = useState(false);
  const folderInputRef = useRef(null);

  // JSX can't reliably set the webkitdirectory IDL property; must assign it directly on the DOM node
  useEffect(() => {
    if (folderInputRef.current) {
      folderInputRef.current.webkitdirectory = true;
      folderInputRef.current.directory = true;
    }
  }, []);
  const [sidExportedThisSession, setSidExportedThisSession] = useState(false);
  const [exportingSidId, setExportingSidId] = useState(null);
  const [checkingBolId, setCheckingBolId] = useState(null);
  const [retryingMatchId, setRetryingMatchId] = useState(null);
  const [acknowledgingMismatchId, setAcknowledgingMismatchId] = useState(null);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [bulkActionLoading, setBulkActionLoading] = useState(false);
  const [bulkFlagOpen, setBulkFlagOpen] = useState(false);
  const [bulkResults, setBulkResults] = useState(null); // { action, succeeded, total, skipped }

  const thirdPartyBols      = pendingBols.filter(b => b.is_third_party);
  const visiblePendingBols  = pendingBols.filter(b => !b.is_third_party);
  const eligibleForDoNotPay = visiblePendingBols.filter(isDoNotPayEligible);

  const readyToReviewBols = visiblePendingBols.filter(b => b.technique_trip != null && b.amount != null);

  const summary = {
    awaitingInvoice:      visiblePendingBols.filter(b => b.technique_trip != null && b.amount == null).length,
    readyToReview:        readyToReviewBols.length,
    readyToReviewTypeA:   readyToReviewBols.filter(b => b.needs_sid_export === true).length,
    readyToReviewTypeB:   readyToReviewBols.filter(b => b.needs_sid_export === false).length,
    approvedToday:        approvedBols.length,
  };

  // selection (multi-select bulk actions)
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

  // sorting (sortable columns)
  function handleSort(column) {
    setSort(prev => {
      if (prev.column !== column) return { column, direction: 'asc' };
      if (prev.direction === 'asc') return { column, direction: 'desc' };
      return { column: null, direction: 'default' };
    });
  }

  function toggleGroupOrder() {
    setGroupOrder(prev => (prev === 'desc' ? 'asc' : 'desc'));
  }

  // fetch helpers
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

  // actions
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

  async function handleRetryMatch(recordId) {
    setRetryingMatchId(recordId);
    try {
      const res = await fetch(`/api/bols/${recordId}/retry-match`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Retry match failed (${res.status})`);
      setSuccessMessage(data.message || (data.matched ? 'Matched.' : 'Still not found.'));
      if (data.matched) await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setRetryingMatchId(null);
    }
  }

  async function handleAcknowledgeMismatch(recordId) {
    setAcknowledgingMismatchId(recordId);
    try {
      const res = await fetch(`/api/bols/${recordId}/acknowledge-mismatch`, { method: 'POST' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Acknowledge failed (${res.status})`);
      }
      await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setAcknowledgingMismatchId(null);
    }
  }

  // automatic follow-up for stubs a CSV upload/poll just created; fires the same
  // retry-match as the manual button, once per stub, each in its own isolated request.
  // returns Map(recordId -> {matched, trip, message}) so the caller can reconcile its summary
  async function autoRetryNewStubs(recordIds) {
    const results = new Map();
    if (!recordIds.length) return results;
    await runWithConcurrency(recordIds, AUTO_RETRY_CONCURRENCY, async id => {
      let data = {};
      for (let attempt = 1; attempt <= AUTO_RETRY_MAX_ATTEMPTS; attempt++) {
        try {
          const res = await fetch(`/api/bols/${id}/retry-match`, { method: 'POST' });
          data = await res.json().catch(() => ({}));
        } catch (err) {
          data = { matched: false, message: err.message };
        }
        // only a timeout is worth another attempt -- a confirmed non-match or real match stops immediately
        if (!data.timed_out || attempt === AUTO_RETRY_MAX_ATTEMPTS) break;
      }
      results.set(id, { matched: !!data.matched, trip: data.matched_trip, message: data.message, timedOut: !!data.timed_out });
    });
    await fetchPending();
    return results;
  }

  // bulk actions: fire the same per-record endpoints as the individual buttons, then one refresh
  function selectedRecords() {
    return visiblePendingBols.filter(b => selectedIds.has(b.id));
  }

  async function handleBulkApprove() {
    const all = selectedRecords();
    if (!all.length) return;
    const targets = all.filter(isApproveEligible); // matches per-row Approve's own gating
    const skipped = all.length - targets.length;
    setBulkActionLoading(true);
    try {
      const results = await Promise.allSettled(
        targets.map(b => fetch(`/api/bols/${b.id}/approve`, { method: 'POST' }))
      );
      const succeeded = results.filter(r => r.status === 'fulfilled' && r.value.ok).length;
      setBulkResults({ action: 'approved', succeeded, total: all.length, skipped });
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
    const eligible = all.filter(isThirdPartyEligible);
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

  async function handleBulkDoNotPay() {
    const all = selectedRecords();
    const eligible = all.filter(isDoNotPayEligible);
    const skipped = all.length - eligible.length;
    if (!all.length) return;
    setBulkActionLoading(true);
    try {
      const results = await Promise.allSettled(
        eligible.map(b => fetch(`/api/bols/${b.id}/mark-do-not-pay`, { method: 'POST' }))
      );
      const succeeded = results.filter(r => r.status === 'fulfilled' && r.value.ok).length;
      setBulkResults({ action: 'marked do-not-pay', succeeded, total: all.length, skipped });
      await Promise.all([fetchPending(), fetchApproved()]);
      clearSelection();
    } catch (err) {
      setError(err.message);
    } finally {
      setBulkActionLoading(false);
    }
  }

  async function handleDoNotPayAllEligible() {
    if (!eligibleForDoNotPay.length) return;
    setBulkActionLoading(true);
    try {
      const results = await Promise.allSettled(
        eligibleForDoNotPay.map(b => fetch(`/api/bols/${b.id}/mark-do-not-pay`, { method: 'POST' }))
      );
      const succeeded = results.filter(r => r.status === 'fulfilled' && r.value.ok).length;
      setBulkResults({ action: 'marked do-not-pay', succeeded, total: eligibleForDoNotPay.length, skipped: 0 });
      await Promise.all([fetchPending(), fetchApproved()]);
    } catch (err) {
      setError(err.message);
    } finally {
      setBulkActionLoading(false);
    }
  }

  async function handleBulkExportSid() {
    const all = selectedRecords();
    const eligible = all.filter(b => b.needs_sid_export && b.manifest && !b.is_third_party && !b.is_do_not_pay);
    const skipped = all.length - eligible.length;
    if (!all.length) return;
    setBulkActionLoading(true);
    try {
      // sequential, not Promise.all -- reduces the chance the browser blocks simultaneous downloads
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

  // shared upload loop -- takes [{file, folderName}] regardless of which picker produced it
  async function uploadInvoiceFiles(fileEntries) {
    if (!fileEntries.length) {
      setError('No CSV files found in the selected folder.');
      return;
    }
    setInvoiceUploading(true);
    setUploadResults(null);
    const matched = [], unmatched = [], errors = [], conflicts = [];
    const newStubIds = [];
    const unmatchedByRecordId = new Map();
    for (let i = 0; i < fileEntries.length; i++) {
      const { file, folderName, pdfFile } = fileEntries[i];
      setUploadProgress(`${i + 1} of ${fileEntries.length}`);
      const form = new FormData();
      form.append('file', file);
      if (pdfFile) form.append('pdf_file', pdfFile);
      if (folderName) form.append('invoice_folder_name', folderName);
      try {
        const res = await fetch('/api/invoices/upload', { method: 'POST', body: form });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          errors.push({ name: file.name, msg: data.detail || `HTTP ${res.status}` });
        } else if (data.matched) {
          matched.push({ name: file.name, invoice: data.invoice_number, trip: data.matched_trip, strategy: data.match_strategy, sender: data.invoice_email_sender });
          if (data.conflict) conflicts.push(data.conflict);
        } else {
          const entry = { name: file.name, invoice: data.invoice_number, jobName: data.job_name, note: data.message, sender: data.invoice_email_sender };
          unmatched.push(entry);
          if (data.match_strategy === 'invoice_only' && data.record_id) {
            newStubIds.push(data.record_id);
            unmatchedByRecordId.set(data.record_id, entry);
            // not final -- the automatic retry below still has to run
            entry.checking = true;
          }
        }
      } catch (err) {
        errors.push({ name: file.name, msg: err.message });
      }
    }
    setInvoiceUploading(false);
    setUploadProgress(null);
    // show results right away -- the retry pass below can take up to a minute; going silent
    // that whole stretch reads as broken, not still working. anything still checking is labeled honestly
    setUploadResults({ matched, unmatched, errors, conflicts });
    await Promise.all([fetchPending(), fetchApproved()]);

    // automatic follow-up: any stub that didn't match already gets one live retry-match now
    if (newStubIds.length) {
      const retryResults = await autoRetryNewStubs(newStubIds);
      const { stillUnmatched, newlyMatched } = reconcileWithRetryResults(unmatched, unmatchedByRecordId, retryResults);
      stillUnmatched.forEach(e => { e.checking = false; });
      matched.push(...newlyMatched);
      setUploadResults({ matched, unmatched: stillUnmatched, errors, conflicts });
    }

    // best-effort: pre-merge this batch's invoice PDFs so "Download Invoices" is instant later
    const sendersInBatch = [...new Set([...matched, ...unmatched].map(r => r.sender).filter(Boolean))];
    await Promise.allSettled(
      sendersInBatch.map(sender =>
        fetch('/api/invoices/merge-batch-pdfs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sender }),
        })
      )
    );
  }

  // primary picker: File System Access API -- each directory handle carries its own real
  // .name, so the immediate-parent folder is always correct with no path-string guessing
  async function pickInvoiceFolder() {
    let rootHandle;
    try {
      rootHandle = await window.showDirectoryPicker();
    } catch (err) {
      if (err.name !== 'AbortError') setError('Folder selection failed: ' + err.message);
      return;
    }
    const fileEntries = [];
    // pair by the leading Z-number, not the full stem -- ALG's PDF filenames carry a suffix the CSVs don't
    const zNumberOf = (name) => {
      const m = name.match(/^\s*(Z\d+)/i);
      return m ? m[1].toUpperCase() : null;
    };
    async function walk(dirHandle, folderName) {
      // pairing is scoped per-directory -- a companion PDF lives in the same folder as its CSV
      const csvEntries = [];
      const pdfByZ = new Map();
      const subdirs = [];
      for await (const entry of dirHandle.values()) {
        if (entry.kind === 'file') {
          const lower = entry.name.toLowerCase();
          if (lower.endsWith('.csv')) {
            csvEntries.push(entry);
          } else if (lower.endsWith('.pdf')) {
            const z = zNumberOf(entry.name);
            if (z) pdfByZ.set(z, entry);
          }
        } else if (entry.kind === 'directory') {
          subdirs.push(entry);
        }
      }
      for (const csvEntry of csvEntries) {
        const file = await csvEntry.getFile();
        const z = zNumberOf(csvEntry.name);
        const pdfEntry = z ? pdfByZ.get(z) : undefined;
        const pdfFile = pdfEntry ? await pdfEntry.getFile() : undefined;
        fileEntries.push({ file, folderName, pdfFile });
      }
      for (const subdir of subdirs) {
        await walk(subdir, subdir.name);
      }
    }
    try {
      await walk(rootHandle, rootHandle.name);
    } catch (err) {
      setError('Failed to read the selected folder: ' + err.message);
      return;
    }
    await uploadInvoiceFiles(fileEntries);
  }

  // fallback for non-Chromium browsers; sender/date is the file's immediate parent folder, derived per file
  function parentFolderName(file) {
    if (!file.webkitRelativePath) return '';
    const parts = file.webkitRelativePath.split(/[\\/]/).filter(Boolean);
    return parts.length >= 2 ? parts[parts.length - 2].trim() : '';
  }

  function handleFolderPickerClick() {
    if (window.showDirectoryPicker) {
      pickInvoiceFolder();
    } else {
      folderInputRef.current?.click();
    }
  }

  async function handleInvoiceUpload(e) {
    const allFiles = Array.from(e.target.files || []);
    if (!allFiles.length) return;
    e.target.value = '';
    const files = allFiles.filter(f => f.name.toLowerCase().endsWith('.csv'));
    if (!files.some(f => f.webkitRelativePath)) {
      setError('The browser did not report folder paths for these files — it may have opened a plain file picker instead of a folder picker. Sender/date will not be auto-detected; try again with the folder picker.');
    }
    // pair each CSV with its companion PDF by leading Z-number, scoped per-folder to avoid cross-pairing
    const zNumberOf = (name) => {
      const m = name.match(/^\s*(Z\d+)/i);
      return m ? m[1].toUpperCase() : null;
    };
    const pdfByKey = new Map();
    for (const f of allFiles) {
      if (f.name.toLowerCase().endsWith('.pdf')) {
        const z = zNumberOf(f.name);
        if (z) pdfByKey.set(`${parentFolderName(f)}::${z}`, f);
      }
    }
    await uploadInvoiceFiles(files.map(file => {
      const folderName = parentFolderName(file);
      const z = zNumberOf(file.name);
      return { file, folderName, pdfFile: z ? pdfByKey.get(`${folderName}::${z}`) : undefined };
    }));
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

  async function handleMoveThirdPartyToLog() {
    const targets = thirdPartyBols;
    if (!targets.length) return;
    const confirmed = window.confirm(
      `Move ${targets.length} third-party record${targets.length !== 1 ? 's' : ''} to the log? This skips the Approved/Send-to-Accounting step.`
    );
    if (!confirmed) return;
    setMovingToLogLoading(true);
    try {
      const approveResults = await Promise.allSettled(
        targets.map(b => fetch(`/api/bols/${b.id}/approve`, { method: 'POST' }))
      );
      const succeededIds = targets
        .filter((b, i) => approveResults[i].status === 'fulfilled' && approveResults[i].value.ok)
        .map(b => b.id);
      if (succeededIds.length) {
        const res = await fetch('/api/bols/mark-accounting-sent', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ record_ids: succeededIds }),
        });
        if (!res.ok) throw new Error(`Move to log failed (${res.status})`);
      }
      const skipped = targets.length - succeededIds.length;
      setBulkResults({ action: 'moved to log', succeeded: succeededIds.length, total: targets.length, skipped });
      await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setMovingToLogLoading(false);
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
        const newStubIds = [];
        const unmatchedByRecordId = new Map();
        for (const r of (data.processed || [])) {
          if (r.error) {
            errors.push({ name: r.filename || r.invoice_number || '?', msg: r.error });
          } else if (r.matched && r.match_strategy !== 'invoice_only') {
            matched.push({ name: r.invoice_number, invoice: r.invoice_number, trip: r.matched_trip, strategy: r.match_strategy });
            if (r.conflict) conflicts.push(r.conflict);
          } else {
            const entry = { name: r.invoice_number, invoice: r.invoice_number, jobName: r.job_name, note: r.message };
            unmatched.push(entry);
            if (r.match_strategy === 'invoice_only' && r.record_id) {
              newStubIds.push(r.record_id);
              unmatchedByRecordId.set(r.record_id, entry);
              entry.checking = true;
            }
          }
        }
        // show results right away -- see uploadInvoiceFiles()'s comment on why
        setPollResults({ matched, unmatched, errors, conflicts });
        await Promise.all([fetchPending(), fetchApproved()]);
        if (newStubIds.length) {
          const retryResults = await autoRetryNewStubs(newStubIds);
          const { stillUnmatched, newlyMatched } = reconcileWithRetryResults(unmatched, unmatchedByRecordId, retryResults);
          stillUnmatched.forEach(e => { e.checking = false; });
          matched.push(...newlyMatched);
          setPollResults({ matched, unmatched: stillUnmatched, errors, conflicts });
        }
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setPollFolderLoading(false);
    }
  }

  async function handleRefetchBols(manifestNumbers) {
    const res = await fetch('/api/admin/refetch-bols', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest_numbers: manifestNumbers }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Re-fetch failed (${res.status})`);
    }
    const data = await res.json();
    if (data.updated?.length) {
      // Update bol_number in local approved state without a full refetch
      const bolMap = Object.fromEntries(data.updated.map(u => [u.manifest, u.bol_number]));
      setApprovedBols(prev => prev.map(b =>
        b.manifest && bolMap[b.manifest] ? { ...b, bol_number: bolMap[b.manifest], needs_sid_export: false } : b
      ));
      setSuccessMessage(`Updated ${data.updated.length} BOL number(s).`);
    } else {
      setSuccessMessage('No new BOL numbers found — check again after Prophecy import completes.');
    }
  }

  async function handleMarkSent(recordIds) {
    setApprovedBols(prev => prev.filter(b => !recordIds.includes(b.id)));
    setSuccessMessage(`${recordIds.length} record(s) marked as sent — moved to Log.`);
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
      setCompareTargetId(null);
      await fetchPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setReassignSubmitting(false);
    }
  }

  // manual correction for a sender batch whose folder name failed to parse into a date
  async function handleFixInvoiceSender(rawSender, correctedFolderName) {
    setFixSenderSubmitting(true);
    try {
      const res = await fetch('/api/invoices/fix-sender', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ raw_sender: rawSender, corrected_folder_name: correctedFolderName }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Correction failed (${res.status})`);
      setSuccessMessage(`Corrected ${data.updated} record(s) to "${data.invoice_email_sender}".`);
      await Promise.all([fetchPending(), fetchApproved()]);
    } catch (err) {
      setError(err.message);
    } finally {
      setFixSenderSubmitting(false);
    }
  }

  // returns true/false rather than throwing, so Compare can update its own candidate list in place
  async function handleDismissSibling(recordId) {
    try {
      const res = await fetch(`/api/bols/${recordId}/dismiss`, { method: 'POST' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Dismiss failed (${res.status})`);
      }
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    }
  }

  async function handleDoNotPay(recordId, shouldMark) {
    setMarkingDoNotPayId(recordId);
    try {
      const route = shouldMark ? 'mark-do-not-pay' : 'unmark-do-not-pay';
      const res = await fetch(`/api/bols/${recordId}/${route}`, { method: 'POST' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `${route} failed (${res.status})`);
      }
      setReassignTargetId(null);
      await Promise.all([fetchPending(), fetchApproved()]);
    } catch (err) {
      setError(err.message);
    } finally {
      setMarkingDoNotPayId(null);
    }
  }

  // render
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
              awaitingInvoice={summary.awaitingInvoice}
              readyToReview={summary.readyToReview}
              readyToReviewTypeA={summary.readyToReviewTypeA}
              readyToReviewTypeB={summary.readyToReviewTypeB}
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
              <span>{pendingBols.length + approvedBols.length} records loaded</span>
              <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
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
                <button
                  onClick={handleFolderPickerClick}
                  disabled={invoiceUploading}
                  title="Select the sender's dated invoice folder (or the whole invoice share) to upload"
                  style={{
                    display: 'inline-block',
                    background: invoiceUploading ? '#e5e7eb' : '#f0f9ff',
                    color: invoiceUploading ? '#9ca3af' : '#0369a1',
                    border: '1px solid #bae6fd',
                    borderRadius: 5,
                    padding: '4px 12px',
                    fontWeight: 600,
                    fontSize: 12,
                    cursor: invoiceUploading ? 'not-allowed' : 'pointer',
                  }}
                >
                  {invoiceUploading ? `Uploading ${uploadProgress}…` : 'Upload Invoice Folder'}
                </button>
                <input
                  ref={folderInputRef}
                  type="file"
                  multiple
                  style={{ display: 'none' }}
                  disabled={invoiceUploading}
                  onChange={handleInvoiceUpload}
                />
              </span>
            </div>

            {uploadResults && (uploadResults.matched.length + uploadResults.unmatched.length + uploadResults.errors.length > 0) && (
              <div style={{ marginBottom: 16, border: '1px solid #e5e7eb', borderRadius: 6, overflow: 'hidden', fontSize: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 12px', background: '#f9fafb', borderBottom: '1px solid #e5e7eb' }}>
                  <span style={{ fontWeight: 600, color: '#374151' }}>
                    Invoice Upload Results — {uploadResults.matched.length} matched &nbsp;·&nbsp; {uploadResults.unmatched.length} unmatched
                    {uploadResults.unmatched.some(r => r.checking) && ` (${uploadResults.unmatched.filter(r => r.checking).length} checking Technique…)`}
                    &nbsp;·&nbsp; {uploadResults.errors.length} errors
                  </span>
                  <button onClick={() => setUploadResults(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#9ca3af', fontSize: 14, lineHeight: 1 }}>✕</button>
                </div>
                {uploadResults.matched.map((r, i) => (
                  <div key={i} style={{ display: 'flex', gap: 12, padding: '5px 12px', background: '#f0fdf4', borderBottom: '1px solid #dcfce7', alignItems: 'center' }}>
                    <span style={{ color: '#16a34a', fontWeight: 700, minWidth: 14 }}>✓</span>
                    <span style={{ fontWeight: 600, color: '#166534', minWidth: 90 }}>{r.invoice}</span>
                    <span style={{ color: '#374151' }}>{r.name}</span>
                    <span style={{ color: r.sender ? '#6b7280' : '#dc2626', fontStyle: r.sender ? 'normal' : 'italic', fontSize: 12 }}>
                      {r.sender || '⚠ no sender detected'}
                    </span>
                    <span style={{ marginLeft: 'auto', color: '#6b7280' }}>→ {r.trip} <span style={{ background: '#dcfce7', color: '#166534', borderRadius: 3, padding: '1px 5px', fontSize: 11 }}>{r.strategy}</span></span>
                  </div>
                ))}
                {uploadResults.unmatched.map((r, i) => (
                  <div key={i} style={{ display: 'flex', gap: 12, padding: '5px 12px', background: r.checking ? '#eff6ff' : '#fffbeb', borderBottom: r.checking ? '1px solid #bfdbfe' : '1px solid #fef3c7', alignItems: 'center' }}>
                    <span style={{ color: r.checking ? '#2563eb' : '#d97706', fontWeight: 700, minWidth: 14 }}>{r.checking ? '⏳' : '—'}</span>
                    <span style={{ fontWeight: 600, color: r.checking ? '#1e40af' : '#92400e', minWidth: 90 }}>{r.invoice}</span>
                    <span style={{ color: '#374151' }}>{r.name}</span>
                    <span style={{ color: r.sender ? '#6b7280' : '#dc2626', fontStyle: r.sender ? 'normal' : 'italic', fontSize: 12 }}>
                      {r.sender || '⚠ no sender detected'}
                    </span>
                    <span style={{ marginLeft: 'auto', color: r.checking ? '#2563eb' : '#6b7280', fontStyle: 'italic' }}>
                      {r.checking ? 'Checking Technique for a match…' : r.note}
                    </span>
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
                    Pull Invoices Results — {pollResults.matched.length} matched &nbsp;·&nbsp; {pollResults.unmatched.length} unmatched
                    {pollResults.unmatched.some(r => r.checking) && ` (${pollResults.unmatched.filter(r => r.checking).length} checking Technique…)`}
                    &nbsp;·&nbsp; {pollResults.errors.length} errors
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
                  <div key={i} style={{ display: 'flex', gap: 12, padding: '5px 12px', background: r.checking ? '#eff6ff' : '#fffbeb', borderBottom: r.checking ? '1px solid #bfdbfe' : '1px solid #fef3c7', alignItems: 'center' }}>
                    <span style={{ color: r.checking ? '#2563eb' : '#d97706', fontWeight: 700, minWidth: 14 }}>{r.checking ? '⏳' : '—'}</span>
                    <span style={{ fontWeight: 600, color: r.checking ? '#1e40af' : '#92400e', minWidth: 90 }}>{r.invoice}</span>
                    <span style={{ marginLeft: 'auto', color: r.checking ? '#2563eb' : '#6b7280', fontStyle: 'italic' }}>
                      {r.checking ? 'Checking Technique for a match…' : r.note}
                    </span>
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
              markingDoNotPayId={markingDoNotPayId}
              exportingSidId={exportingSidId}
              checkingBolId={checkingBolId}
              retryingMatchId={retryingMatchId}
              acknowledgingMismatchId={acknowledgingMismatchId}
              filterText={filterText}
              onFilterChange={setFilterText}
              selectedIds={selectedIds}
              onToggleSelect={toggleSelect}
              onToggleSelectAll={toggleSelectAll}
              sort={sort}
              onSort={handleSort}
              groupOrder={groupOrder}
              onToggleGroupOrder={toggleGroupOrder}
              fixSenderSubmitting={fixSenderSubmitting}
              onFixInvoiceSender={handleFixInvoiceSender}
              onApprove={handleApprove}
              onFlagOpen={setFlagTarget}
              onUnflag={handleUnflag}
              onNotesUpdate={handleNotesUpdate}
              onMarkThirdParty={handleMarkThirdParty}
              onReassignOpen={id => setReassignTargetId(id)}
              onCompareOpen={id => setCompareTargetId(id)}
              onAcknowledgeMismatch={handleAcknowledgeMismatch}
              onDoNotPay={handleDoNotPay}
              onExportSid={handleExportRecordToProphecy}
              onCheckBol={handleCheckBol}
              onRetryMatch={handleRetryMatch}
            />

            {eligibleForDoNotPay.length > 0 && (
              <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
                <button
                  onClick={handleDoNotPayAllEligible}
                  disabled={bulkActionLoading}
                  title="Mark every remaining eligible invoice-only record as Do Not Pay"
                  style={{
                    background: bulkActionLoading ? '#e5e7eb' : '#f3f4f6',
                    color: bulkActionLoading ? '#9ca3af' : '#374151',
                    border: '1px solid #d1d5db',
                    borderRadius: 5,
                    padding: '5px 12px',
                    fontWeight: 600,
                    fontSize: 12,
                    cursor: bulkActionLoading ? 'not-allowed' : 'pointer',
                  }}
                >
                  {bulkActionLoading ? 'Marking…' : `Do Not Pay All (${eligibleForDoNotPay.length})`}
                </button>
              </div>
            )}

            <ThirdPartySection
              bols={thirdPartyBols}
              unmarkingThirdPartyId={unmarkingThirdPartyId}
              movingToLogLoading={movingToLogLoading}
              onUnmark={handleUnmarkThirdParty}
              onMoveAllToLog={handleMoveThirdPartyToLog}
            />

            <ApprovedSection
              approvedBols={approvedBols}
              loading={loadingApproved && approvedBols.length === 0}
              sidLoading={sidLoading}
              sidExportedThisSession={sidExportedThisSession}
              unapprovingId={unapprovingId}
              undoingDoNotPayId={markingDoNotPayId}
              onUnapprove={handleUnapprove}
              onUndoDoNotPay={id => handleDoNotPay(id, false)}
              onExportProphecy={handleExportProphecy}
              onRefetchBols={handleRefetchBols}
              onMarkSent={handleMarkSent}
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
          onDoNotPay={handleDoNotPay}
        />
      )}

      {compareTargetId && (
        <CompareManifestsModal
          bol={pendingBols.find(b => b.id === compareTargetId) || null}
          submitting={reassignSubmitting}
          onClose={() => setCompareTargetId(null)}
          onReassign={handleReassignInvoice}
          onDismiss={handleDismissSibling}
        />
      )}

      <BulkActionToolbar
        count={selectedIds.size}
        loading={bulkActionLoading}
        onApprove={handleBulkApprove}
        onFlag={openBulkFlag}
        onMarkThirdParty={handleBulkMarkThirdParty}
        onDoNotPay={handleBulkDoNotPay}
        onExportSid={handleBulkExportSid}
        onClear={clearSelection}
      />
    </div>
  );
}
