import argparse
import email
import getpass
import imaplib
import os
import socket
import webbrowser
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


if load_dotenv:
    # Load environment variables from .env in the project root if available.
    load_dotenv()


def clean(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text).strip("_") or "email"


def decode_mime_header(value: str | None, fallback: str) -> str:
    if not value:
        return fallback

    decoded_parts = []
    for chunk, encoding in decode_header(value):
        if isinstance(chunk, bytes):
            charset = encoding or "utf-8"
            try:
                decoded_parts.append(chunk.decode(charset, errors="replace"))
            except LookupError:
                decoded_parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(chunk)
    return "".join(decoded_parts).strip() or fallback


def decode_part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""

    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def ensure_folder(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def save_attachment(part: Message, folder: str) -> None:
    filename = decode_mime_header(part.get_filename(), "attachment.bin")
    basename = os.path.basename(filename)
    stem, ext = os.path.splitext(basename)
    safe_name = clean(stem) + ext
    data = part.get_payload(decode=True)
    if not data:
        return

    ensure_folder(folder)
    attachment_path = os.path.join(folder, safe_name)
    with open(attachment_path, "wb") as f:
        f.write(data)
    print(f"Saved attachment: {attachment_path}")


def process_message(msg: Message, index: int, args: argparse.Namespace) -> None:
    subject = decode_mime_header(msg.get("Subject"), "(No Subject)")
    sender = decode_mime_header(msg.get("From"), "(Unknown Sender)")
    
    # Extract and format email date
    date_header = msg.get("Date")
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
            date_str = dt.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            date_str = "unknown-date"
    else:
        date_str = "unknown-date"
    
    email_folder = os.path.join("mail", clean(f"{date_str}_{subject}"))
    html_body = ""

    print(f"Subject: {subject}")
    print(f"From: {sender}")

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue

            disposition = part.get_content_disposition()
            content_type = part.get_content_type()

            if disposition == "attachment":
                if args.download_attachments:
                    save_attachment(part, email_folder)
                continue

            if content_type == "text/plain":
                text_body = decode_part_text(part).strip()
                if text_body:
                    print(text_body)
            elif content_type == "text/html" and not html_body:
                html_body = decode_part_text(part)
    else:
        content_type = msg.get_content_type()
        body = decode_part_text(msg)
        if content_type == "text/plain" and body.strip():
            print(body.strip())
        elif content_type == "text/html":
            html_body = body

    if args.save_html and html_body.strip():
        ensure_folder(email_folder)
        html_path = os.path.join(email_folder, "index.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        print(f"Saved HTML body: {html_path}")
        if args.open_html:
            webbrowser.open(html_path)

    print("=" * 100)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and parse recent emails over IMAP.")
    parser.add_argument("--imap-server", default=os.getenv("IMAP_SERVER"), help="IMAP server hostname")
    parser.add_argument("--mailbox", default="INBOX", help="Mailbox name to read")
    parser.add_argument("--limit", type=int, default=3, help="Number of newest emails to fetch")
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Network timeout in seconds for IMAP operations",
    )
    parser.add_argument("--username", help="Email username (overrides env vars)")
    parser.add_argument("--password", help="Email password or app password (overrides env vars)")
    parser.add_argument(
        "--download-attachments",
        action="store_true",
        help="Download attachments to per-email folders",
    )
    parser.add_argument(
        "--save-html",
        action="store_true",
        help="Save first HTML body found in each email",
    )
    parser.add_argument(
        "--open-html",
        action="store_true",
        help="Open saved HTML files in the browser (implies --save-html)",
    )
    return parser.parse_args()


def get_credentials(args: argparse.Namespace) -> tuple[str, str]:
    username = args.username or os.getenv("EMAIL_USER") or os.getenv("email")
    password = args.password or os.getenv("EMAIL_PASS") or os.getenv("password")

    if not username:
        username = input("Email username: ").strip()
    if not password:
        password = getpass.getpass("Email password/app password: ")

    if not username or not password:
        raise ValueError("Missing credentials. Provide --username/--password or set env vars.")
    return username, password


def main() -> None:
    args = parse_args()
    if args.open_html:
        args.save_html = True

    username, password = get_credentials(args)
    timeout_seconds = max(args.timeout, 1)
    print(f"Connecting to {args.imap_server} (timeout={timeout_seconds}s)...")
    imap = imaplib.IMAP4_SSL(args.imap_server, timeout=timeout_seconds)

    try:
        print("Logging in...")
        imap.login(username, password)
        print(f"Selecting mailbox: {args.mailbox}")
        status, _ = imap.select(args.mailbox)
        if status != "OK":
            raise RuntimeError(f"Unable to select mailbox: {args.mailbox}")

        print("Searching messages...")
        if os.getenv("plusaddress"):
            plus_address = os.getenv("plusaddress")
            status, data = imap.search(None, "TO", f"{username.split('@')[0]}+{plus_address}@{username.split('@')[1]}")
        else:
            status, data = imap.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            print("No emails found.")
            return

        message_ids = data[0].split()
        limit = max(args.limit, 0)
        selected_ids = message_ids[-limit:]
        print(f"Found {len(message_ids)} messages. Processing latest {len(selected_ids)}.")

        for position, message_id in enumerate(reversed(selected_ids), start=1):
            print(f"Fetching message {position}/{len(selected_ids)} (id={message_id.decode(errors='ignore')})")
            status, fetched = imap.fetch(message_id, "(RFC822)")
            if status != "OK" or not fetched:
                print(f"Skipping message ID {message_id!r}: fetch failed")
                continue

            for response in fetched:
                if isinstance(response, tuple):
                    parsed_message = email.message_from_bytes(response[1])
                    process_message(parsed_message, position, args)

    except socket.timeout as exc:
        raise TimeoutError(
            "IMAP operation timed out. Try a higher --timeout value (for example, --timeout 90)."
        ) from exc

    finally:
        try:
            imap.close()
        except imaplib.IMAP4.error:
            pass
        imap.logout()


if __name__ == "__main__":
    main()