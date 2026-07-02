const BTN = {
  border: 'none',
  borderRadius: 6,
  padding: '7px 14px',
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
};

export default function BulkActionToolbar({
  count, loading,
  onApprove, onFlag, onMarkThirdParty, onIgnore, onExportSid, onClear,
}) {
  if (!count) return null;

  return (
    <div
      style={{
        position: 'fixed',
        bottom: 24,
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: 900,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        background: '#1A1A1A',
        color: '#fff',
        borderRadius: 10,
        padding: '10px 14px',
        boxShadow: '0 12px 32px rgba(0,0,0,0.35)',
      }}
    >
      <span style={{ fontSize: 13, fontWeight: 700, padding: '0 6px', whiteSpace: 'nowrap' }}>
        {count} selected
      </span>

      <button
        onClick={onApprove}
        disabled={loading}
        title="Approve all selected records"
        style={{ ...BTN, background: loading ? '#374151' : '#2D6A4F', color: '#fff', opacity: loading ? 0.6 : 1 }}
      >
        ✓ Approve
      </button>

      <button
        onClick={onFlag}
        disabled={loading}
        title="Flag all selected records with one shared reason"
        style={{ ...BTN, background: loading ? '#374151' : '#fff7ed', color: loading ? '#9ca3af' : '#92400e', opacity: loading ? 0.6 : 1 }}
      >
        ⚑ Flag
      </button>

      <button
        onClick={onMarkThirdParty}
        disabled={loading}
        title="Mark eligible selected records as third-party"
        style={{ ...BTN, background: loading ? '#374151' : '#fff7ed', color: loading ? '#9ca3af' : '#c2410c', opacity: loading ? 0.6 : 1 }}
      >
        3P
      </button>

      <button
        onClick={onIgnore}
        disabled={loading}
        title="Ignore eligible selected records (invoice-only stubs)"
        style={{ ...BTN, background: loading ? '#374151' : '#f3f4f6', color: loading ? '#9ca3af' : '#374151', opacity: loading ? 0.6 : 1 }}
      >
        Ignore
      </button>

      <button
        onClick={onExportSid}
        disabled={loading}
        title="Export Prophecy SID for eligible selected Type A records (one download each)"
        style={{ ...BTN, background: loading ? '#374151' : '#eff6ff', color: loading ? '#9ca3af' : '#1e40af', opacity: loading ? 0.6 : 1 }}
      >
        SID
      </button>

      <button
        onClick={onClear}
        disabled={loading}
        title="Clear selection"
        style={{ ...BTN, background: 'none', color: '#9ca3af', padding: '7px 8px', fontSize: 15 }}
      >
        ✕
      </button>
    </div>
  );
}
