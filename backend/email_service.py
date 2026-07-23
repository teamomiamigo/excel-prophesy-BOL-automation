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
