// Groups records by invoice_email_sender -- the same key/shape ApprovedSection.jsx's
// local groupByBatch() has used for its per-sender batch cards. Pulled out here so
// BOLTable.jsx (Pending table) can group identically; ApprovedSection.jsx keeps its
// own copy untouched.
export function groupBySender(records) {
  const groups = {};
  for (const r of records) {
    const key = r.invoice_email_sender || '__unassigned__';
    if (!groups[key]) {
      groups[key] = {
        key,
        label: r.invoice_email_sender || 'No Sender',
        sentAt: r.invoice_sent_at || null,
        records: [],
      };
    }
    if (r.invoice_sent_at && (!groups[key].sentAt || r.invoice_sent_at > groups[key].sentAt)) {
      groups[key].sentAt = r.invoice_sent_at;
    }
    groups[key].records.push(r);
  }
  return Object.values(groups);
}

// direction 'desc' (default) = newest sentAt first; 'asc' = oldest first.
// Null-sentAt groups always sink to the bottom regardless of direction.
export function sortGroupsByRecency(groups, direction = 'desc') {
  return groups.slice().sort((a, b) => {
    if (!a.sentAt && !b.sentAt) return 0;
    if (!a.sentAt) return 1;
    if (!b.sentAt) return -1;
    return direction === 'asc'
      ? a.sentAt.localeCompare(b.sentAt)
      : b.sentAt.localeCompare(a.sentAt);
  });
}
