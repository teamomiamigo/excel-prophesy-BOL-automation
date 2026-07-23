# IMAP polling for ALG invoice emails
import email as email_lib
import imaplib
import logging
from email.header import decode_header as _decode_header

logger = logging.getLogger(__name__)


def _decode_str(value) -> str:
    # decode email header value to plain string
    if value is None:
        return ""
    parts = _decode_header(value)
    result = []
    for decoded, charset in parts:
        if isinstance(decoded, bytes):
            result.append(decoded.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(decoded))
    return "".join(result)


def poll_alg_invoice_emails(
    imap_host: str,
    imap_port: int,
    username: str,
    password: str,
    sender_filter: str = "",
    mailbox: str = "INBOX",
) -> list[tuple[str, bytes]]:
    # connect to IMAP via SSL, find unread emails (optionally filtered by sender)  and ext ract .csv attachments, and mark processes as read
    """
    Returns list of (filename, csv_bytes) tuples — one entry per attachment.
    A single email may contribute multiple tuples if it has multiple CSVs.
    """
    results: list[tuple[str, bytes]] = []

    with imaplib.IMAP4_SSL(imap_host, imap_port) as imap:
        imap.login(username, password)
        status, _ = imap.select(mailbox)
        if status != "OK":
            raise RuntimeError(f"Cannot select mailbox '{mailbox}'")

        criteria = f'(UNSEEN FROM "{sender_filter}")' if sender_filter else "UNSEEN"
        _, data = imap.search(None, criteria)
        msg_ids = data[0].split() if data and data[0] else []
        logger.info(
            "[EMAIL POLL] mailbox=%s criteria=%s → %d message(s)",
            mailbox, criteria, len(msg_ids),
        )

        for msg_id in msg_ids:
            _, msg_data = imap.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1] if msg_data and msg_data[0] else None
            if not raw:
                continue

            msg = email_lib.message_from_bytes(raw)
            subject = _decode_str(msg.get("Subject", ""))
            sender = _decode_str(msg.get("From", ""))
            logger.info("[EMAIL POLL] msg=%s from=%s subject=%s", msg_id, sender, subject)

            csv_found = False
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                fname = part.get_filename()
                if not fname:
                    continue
                fname = _decode_str(fname)
                if not fname.lower().endswith(".csv"):
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    results.append((fname, payload))
                    csv_found = True
                    logger.info("[EMAIL POLL] extracted %s (%d bytes)", fname, len(payload))

            # Mark read regardless of attachments — avoids re-scanning non-invoice emails
            imap.store(msg_id, "+FLAGS", "\\Seen")
            if not csv_found:
                logger.info("[EMAIL POLL] msg=%s had no .csv attachments — marked read", msg_id)

    return results
