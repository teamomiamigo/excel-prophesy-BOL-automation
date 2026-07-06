export default function SummaryBar({ awaitingInvoice, readyToReview, readyToReviewTypeA, readyToReviewTypeB, approvedToday }) {
  const cards = [
    { label: 'Awaiting Invoice', value: awaitingInvoice, color: '#6b7280', bg: '#f9fafb' },
    {
      label: 'Ready to Review',
      value: readyToReview,
      color: '#E76F1E',
      bg: '#fff7ed',
      sub: `${readyToReviewTypeA} Type A · ${readyToReviewTypeB} Type B`,
    },
    { label: 'Approved Today', value: approvedToday, color: '#2D6A4F', bg: '#f0fdf4' },
  ];

  return (
    <div style={{ display: 'flex', gap: 16, marginBottom: 20 }}>
      {cards.map(card => (
        <div key={card.label} style={{
          background: card.bg,
          border: `1px solid ${card.color}33`,
          borderRadius: 8,
          padding: '14px 20px',
          minWidth: 160,
          flex: 1,
        }}>
          <div style={{ fontSize: 28, fontWeight: 700, color: card.color, lineHeight: 1 }}>
            {card.value}
          </div>
          <div style={{ fontSize: 12, color: '#6b7280', marginTop: 4, fontWeight: 500 }}>
            {card.label}
          </div>
          {card.sub && (
            <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 3 }}>
              {card.sub}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
