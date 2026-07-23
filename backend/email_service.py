import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import date
from typing import Optional

from backend.config import settings
from backend.csv_export import generate_csv_bytes, get_csv_filename

logger = logging.getLogger(__name__)


def send_bol_export_email(
    bol_records: list[dict],
    export_date: Optional[date] = None,
    recipients: Optional[list[str]] = None,
) -> bool:
    # Generate the approved-BOL CSV and send it to Mary and Katie.
    # Returns True if the email was sent successfully.
    # Returns False if the email was not sent (e.g., SMTP not configured, or SMTP error).
    d = export_date or date.today()
    recipients = recipients or (settings.EMAIL_TO_MARY + settings.EMAIL_TO_KATIE)
    csv_bytes = generate_csv_bytes(bol_records)
    filename = get_csv_filename(d)
    subject = f"{settings.EMAIL_SUBJECT_PREFIX} {d.strftime('%B %d, %Y')}"

    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.warning(
            "[EMAIL FALLBACK] SMTP not configured. Would have sent %d BOL records to %s as '%s'",
            len(bol_records),
            recipients,
            filename,
        )
        return False

    msg = MIMEMultipart()
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    body = (
        f"Please find attached the approved BOL export for {d.strftime('%B %d, %Y')}.\n\n"
        f"Records: {len(bol_records)}\n"
        f"Attachment: {filename}\n\n"
        f"— SG360 Logistics"
    )
    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(csv_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.EMAIL_FROM, recipients, msg.as_string())
        logger.info("[EMAIL] Sent BOL export to %s — %d records", recipients, len(bol_records))
        return True
    except smtplib.SMTPException as exc:
        logger.error("[EMAIL] SMTP failure: %s", exc)
        return False


def send_agent_summary_email(
    proposals: list[dict],
    poll_result: dict,
    run: dict,
) -> bool:
    """
    Send Katie one summary of everything the AI agent just processed, with a
    one-click "approve all recommended" link — GET /api/agents/email-batch-approve
    only renders a confirmation page; the POST from that page's own form is
    the only thing that ever mutates anything (defends against O365 Safe
    Links or any other prefetcher silently triggering an action via GET).

    Same soft-fail contract as send_bol_export_email(): if SMTP isn't
    configured, log a warning and return False rather than raising — but
    additionally log the full batch-approve URL, since that's what makes the
    local demo work end-to-end without real SMTP credentials configured.
    """
    recipients = settings.EMAIL_TO_KATIE
    batch_url = (
        f"{settings.APP_BASE_URL}/api/agents/email-batch-approve"
        f"?run_id={run['id']}&token={run['email_action_token']}"
    )

    approve = [p for p in proposals if p["recommended_action"] == "approve"]
    review = [p for p in proposals if p["recommended_action"] == "needs_review"]
    flag = [p for p in proposals if p["recommended_action"] == "flag"]

    subject = f"{settings.EMAIL_SUBJECT_PREFIX} AI Agent Summary — {len(proposals)} record(s) reviewed"

    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.warning(
            "[EMAIL FALLBACK] SMTP not configured. Would have sent AI agent summary "
            "to %s — %d recommend-approve / %d needs-review / %d recommend-flag. "
            "Batch-approve link: %s",
            recipients, len(approve), len(review), len(flag), batch_url,
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    text_lines = [
        f"AI Agent finished a run: {poll_result.get('found', 0)} new invoice(s) found, "
        f"{len(proposals)} record(s) classified.",
        f"  Recommend approve: {len(approve)}",
        f"  Needs review:      {len(review)}",
        f"  Recommend flag:    {len(flag)}",
        "",
    ]
    if approve:
        text_lines.append(f"Approve all {len(approve)} recommended in one click: {batch_url}")
        text_lines.append("")
    text_lines += ["Or review each record individually in the Agent Activity tab.", "", "— SG360 BOL AI Agent"]
    msg.attach(MIMEText("\n".join(text_lines), "plain"))

    _action_style = {
        "approve": ("#2D6A4F", "Approve"),
        "needs_review": ("#92400e", "Needs Review"),
        "flag": ("#dc2626", "Flag"),
    }

    def _row(p):
        color, label = _action_style.get(p["recommended_action"], ("#374151", p["recommended_action"]))
        cost_pct = p.get("cost_pct")
        cost_display = f"{float(cost_pct) * 100:.2f}%" if cost_pct is not None else "N/A"
        amount = p.get("amount")
        amount_display = f"${float(amount):,.2f}" if amount is not None else "N/A"
        return f"""
        <tr>
          <td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;">{p.get('invoice_number') or p.get('technique_trip') or '—'}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;">{amount_display}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;">{cost_display}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;color:{color};font-weight:600;">{label}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;font-size:13px;color:#374151;">{p.get('reasoning', '')}</td>
        </tr>
        """

    rows_html = "".join(_row(p) for p in proposals) or (
        "<tr><td colspan='5' style='padding:10px;'>No records to review.</td></tr>"
    )

    button_html = ""
    if approve:
        button_html = f"""
        <p><a href="{batch_url}" style="background:#2D6A4F;color:#fff;text-decoration:none;
           padding:10px 18px;border-radius:6px;font-weight:600;display:inline-block;">
          Approve all {len(approve)} recommended
        </a></p>
        """

    html = f"""
    <html><body style="font-family:-apple-system,sans-serif;color:#111;">
      <h2>SG360 BOL — AI Agent Summary</h2>
      <p>{poll_result.get('found', 0)} new invoice file(s) found &middot; {len(proposals)} record(s) classified</p>
      <p>
        <span style="color:#2D6A4F;font-weight:600;">{len(approve)} recommend approve</span> &nbsp;&middot;&nbsp;
        <span style="color:#92400e;font-weight:600;">{len(review)} needs review</span> &nbsp;&middot;&nbsp;
        <span style="color:#dc2626;font-weight:600;">{len(flag)} recommend flag</span>
      </p>
      {button_html}
      <table style="border-collapse:collapse;width:100%;font-size:14px;">
        <thead>
          <tr style="text-align:left;background:#f9fafb;">
            <th style="padding:6px 10px;">Invoice / Trip</th>
            <th style="padding:6px 10px;">Amount</th>
            <th style="padding:6px 10px;">Cost %</th>
            <th style="padding:6px 10px;">Recommendation</th>
            <th style="padding:6px 10px;">Reasoning</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
      <p style="color:#6b7280;font-size:12px;margin-top:20px;">
        Anything not listed above had no new cost data to evaluate yet.
        Review any remaining record directly in the Agent Activity tab.
      </p>
    </body></html>
    """
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.EMAIL_FROM, recipients, msg.as_string())
        logger.info("[EMAIL] Sent AI agent summary to %s — %d records", recipients, len(proposals))
        return True
    except smtplib.SMTPException as exc:
        logger.error("[EMAIL] SMTP failure sending AI agent summary: %s", exc)
        return False
