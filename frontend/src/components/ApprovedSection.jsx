import { useState } from 'react';
import EmailComposeModal from './EmailComposeModal.jsx';

const TH = {
  padding: '7px 10px',
  background: '#374151',
  color: '#fff',
  textAlign: 'left',
  fontSize: 11,
  fontWeight: 600,
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
  whiteSpace: 'nowrap',
};

const TD = {
  padding: '7px 10px',
  borderBottom: '1px solid #f3f4f6',
  fontSize: 13,
  whiteSpace: 'nowrap',
};

const TD_R = { ...TD, textAlign: 'right' };

function fmtMoney(val) {
  if (val == null) return '—';
  return `$${parseFloat(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtNum(val) {
  if (val == null) return '—';
  return parseInt(val).toLocaleString('en-US');
}

function fmtCostPct(costPct) {
  if (costPct == null) return '—';
  return `${(costPct * 100).toFixed(2)}%`;
}

function groupByBatch(records) {
  const groups = {};
  for (const r of records) {
    const key = r.invoice_email_sender || '__unassigned__';
    if (!groups[key]) {
      groups[key] = {
        label: r.invoice_email_sender || 'No Sender',
        sentAt: r.invoice_sent_at || null,
        records: [],
      };
    }
    // Track the most recent sentAt for this group
    if (r.invoice_sent_at && (!groups[key].sentAt || r.invoice_sent_at > groups[key].sentAt)) {
      groups[key].sentAt = r.invoice_sent_at;
    }
    groups[key].records.push(r);
  }
  // Sort: most recent sentAt first, null sentAt last
  return Object.values(groups).sort((a, b) => {
    if (!a.sentAt && !b.sentAt) return 0;
    if (!a.sentAt) return 1;
    if (!b.sentAt) return -1;
    return b.sentAt.localeCompare(a.sentAt);
  });
}

export default function ApprovedSection({
  approvedBols,
  loading,
  sidLoading,
  sidExportedThisSession,
  unapprovingId,
  onUnapprove,
  onExportProphecy,
  onRefetchBols,
  onMarkSent,
}) {
  const [collapsedGroups, setCollapsedGroups] = useState(new Set());
  const [composeModalGroup, setComposeModalGroup] = useState(null);
  const [refetchingKey, setRefetchingKey] = useState(null);
  const [refetchError, setRefetchError] = useState(null);

  const sidCount = approvedBols.filter(b => b.needs_sid_export).length;
  const batches = groupByBatch(approvedBols);

  function toggleGroup(key) {
    setCollapsedGroups(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function handleRefetchBols(batch) {
    const manifests = batch.records
      .filter(r => r.manifest && !r.bol_number)
      .map(r => r.manifest);
    if (!manifests.length) return;
    setRefetchingKey(batch.label);
    setRefetchError(null);
    try {
      await onRefetchBols(manifests);
    } catch (err) {
      setRefetchError(err.message);
    } finally {
      setRefetchingKey(null);
    }
  }

  return (
    <section>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <h2 style={{ fontSize: 14, fontWeight: 600, color: '#374151', margin: 0 }}>
          Approved ({approvedBols.length})
        </h2>
        {sidCount > 0 && (
          <button
            onClick={onExportProphecy}
            disabled={sidLoading || sidExportedThisSession}
            style={{
              background: sidExportedThisSession ? '#f0fdf4' : '#fffbeb',
              color: sidExportedThisSession ? '#166534' : '#92400e',
              border: `1px solid ${sidExportedThisSession ? '#bbf7d0' : '#fcd34d'}`,
              borderRadius: 6,
              padding: '6px 14px',
              fontWeight: 600,
              fontSize: 12,
              cursor: (sidLoading || sidExportedThisSession) ? 'not-allowed' : 'pointer',
              opacity: sidLoading ? 0.6 : 1,
            }}
          >
            {sidExportedThisSession ? '✓ SID Exported' : sidLoading ? 'Generating…' : `Export to Prophecy (${sidCount})`}
          </button>
        )}
      </div>

      {refetchError && (
        <div style={{
          background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 5,
          padding: '8px 12px', marginBottom: 10, fontSize: 12, color: '#991b1b',
          display: 'flex', justifyContent: 'space-between',
        }}>
          <span>Re-fetch failed: {refetchError}</span>
          <button onClick={() => setRefetchError(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#991b1b' }}>×</button>
        </div>
      )}

      {loading ? (
        <div style={{ padding: 24, textAlign: 'center', color: '#9ca3af' }}>Loading…</div>
      ) : approvedBols.length === 0 ? (
        <div style={{
          padding: 20, textAlign: 'center', color: '#9ca3af',
          background: '#fff', borderRadius: 8, border: '1px solid #e5e7eb', fontSize: 13,
        }}>
          No approved records pending accounting.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {batches.map((batch, idx) => {
            const isExpanded = !collapsedGroups.has(batch.label);
            const total = batch.records.reduce((s, r) => s + (parseFloat(r.amount) || 0), 0);
            const missingBolCount = batch.records.filter(r => r.technique_trip && !r.bol_number).length;
            const isRefetching = refetchingKey === batch.label;

            return (
              <div key={batch.label} style={{ border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden' }}>
                {/* Batch header — click to expand/collapse */}
                <div
                  onClick={() => toggleGroup(batch.label)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    padding: '10px 14px',
                    background: idx === 0 ? '#f0fdf4' : '#f9fafb',
                    cursor: 'pointer',
                    userSelect: 'none',
                  }}
                >
                  <span style={{ fontSize: 12, fontWeight: 700, color: '#111827', flex: 1 }}>
                    {batch.label}
                    <span style={{ fontWeight: 400, color: '#6b7280', marginLeft: 8 }}>
                      · {batch.records.length} record{batch.records.length !== 1 ? 's' : ''} · {fmtMoney(total)}
                    </span>
                  </span>
                  {missingBolCount > 0 && (
                    <span style={{
                      fontSize: 11, background: '#fff7ed', color: '#c2410c',
                      border: '1px solid #fed7aa', borderRadius: 3, padding: '2px 7px', fontWeight: 600,
                    }}>
                      {missingBolCount} missing BOL
                    </span>
                  )}
                  <span style={{ fontSize: 13, color: '#9ca3af' }}>{isExpanded ? '▾' : '▸'}</span>
                </div>

                {/* Action bar */}
                <div style={{
                  display: 'flex', gap: 8, padding: '8px 14px',
                  background: '#fff',
                  borderBottom: isExpanded ? '1px solid #e5e7eb' : 'none',
                  borderTop: '1px solid #e5e7eb',
                }}>
                  {missingBolCount > 0 && (
                    <button
                      onClick={() => handleRefetchBols(batch)}
                      disabled={isRefetching}
                      title="Re-run the Technique query for these manifests to pull BOL numbers created in Prophecy"
                      style={{
                        background: '#f0fdf4',
                        color: '#166534',
                        border: '1px solid #bbf7d0',
                        borderRadius: 5,
                        padding: '5px 12px',
                        fontSize: 12,
                        fontWeight: 600,
                        cursor: isRefetching ? 'not-allowed' : 'pointer',
                        opacity: isRefetching ? 0.6 : 1,
                      }}
                    >
                      {isRefetching ? 'Fetching…' : '↺ Re-fetch BOLs'}
                    </button>
                  )}
                  <button
                    onClick={() => setComposeModalGroup(batch)}
                    style={{
                      background: '#2D6A4F',
                      color: '#fff',
                      border: 'none',
                      borderRadius: 5,
                      padding: '5px 14px',
                      fontSize: 12,
                      fontWeight: 600,
                      cursor: 'pointer',
                      marginLeft: 'auto',
                    }}
                  >
                    Send to Accounting →
                  </button>
                </div>

                {/* Records table */}
                {isExpanded && (
                  <div style={{ overflowX: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff' }}>
                      <thead>
                        <tr>
                          <th style={TH}>Trip</th>
                          <th style={TH}>Manifest</th>
                          <th style={TH}>BOL</th>
                          <th style={{ ...TH, textAlign: 'right' }}>Wgt</th>
                          <th style={{ ...TH, textAlign: 'right' }}>Pallets</th>
                          <th style={{ ...TH, textAlign: 'right' }}>PCS</th>
                          <th style={TH}>Invoice #</th>
                          <th style={{ ...TH, textAlign: 'right' }}>Calc Cost</th>
                          <th style={{ ...TH, textAlign: 'right' }}>Amount</th>
                          <th style={{ ...TH, textAlign: 'right' }}>Cost %</th>
                          <th style={{ ...TH, textAlign: 'center' }}>Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {batch.records.map(bol => {
                          const isUnapproving = unapprovingId === bol.id;
                          return (
                            <tr key={bol.id} style={{ background: '#f0fdf4' }}>
                              <td style={TD}>{bol.technique_trip || '—'}</td>
                              <td style={TD}>{bol.manifest || '—'}</td>
                              <td style={TD}>
                                {bol.bol_number ?? <span style={{ color: '#9ca3af' }}>—</span>}
                              </td>
                              <td style={TD_R}>{fmtNum(bol.technique_weight)}</td>
                              <td style={TD_R}>{fmtNum(bol.technique_pallets)}</td>
                              <td style={TD_R}>{fmtNum(bol.technique_pcs)}</td>
                              <td style={{ ...TD, fontWeight: 600 }}>{bol.invoice_number || '—'}</td>
                              <td style={TD_R}>{fmtMoney(bol.access_prog)}</td>
                              <td style={{ ...TD_R, fontWeight: 600 }}>{fmtMoney(bol.amount)}</td>
                              <td style={{ ...TD_R, color: '#16a34a', fontWeight: 600 }}>
                                {fmtCostPct(bol.cost_pct)}
                              </td>
                              <td style={{ ...TD, textAlign: 'center' }}>
                                <button
                                  onClick={() => onUnapprove(bol.id)}
                                  disabled={isUnapproving}
                                  style={{
                                    background: isUnapproving ? '#f3f4f6' : '#fff',
                                    color: isUnapproving ? '#9ca3af' : '#6b7280',
                                    border: '1px solid #d1d5db',
                                    borderRadius: 4,
                                    padding: '4px 10px',
                                    fontSize: 12,
                                    fontWeight: 600,
                                    cursor: isUnapproving ? 'not-allowed' : 'pointer',
                                  }}
                                >
                                  {isUnapproving ? '…' : '↩ Revert'}
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {composeModalGroup && (
        <EmailComposeModal
          records={composeModalGroup.records}
          senderLabel={composeModalGroup.label}
          onClose={() => setComposeModalGroup(null)}
          onMarkSent={ids => {
            setComposeModalGroup(null);
            onMarkSent(ids);
          }}
        />
      )}
    </section>
  );
}
