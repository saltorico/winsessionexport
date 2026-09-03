"""Windows-safe core for exporting one Session Desktop conversation.

The GUI imports this module.  Cryptographic dependencies are imported lazily so
the non-cryptographic unit tests can run on any development machine.
"""

from __future__ import annotations

import binascii
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from mimetypes import guess_extension
from pathlib import Path
from typing import Callable, Mapping, Sequence


HEX_KEY_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "application/pdf": ".pdf",
}
INLINE_IMAGE = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}
INLINE_VIDEO = {"video/mp4", "video/quicktime", "video/webm"}
INLINE_AUDIO = {"audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/ogg", "audio/wav"}


class ExportError(RuntimeError):
    """An expected, user-displayable export failure."""


@dataclass(frozen=True)
class SessionProfile:
    root: Path
    database: Path
    config: Path
    attachments: Path


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    label: str
    visible_message_count: int = -1
    latest_timestamp: int | str | None = None
    latest_preview: str = ""

    @property
    def display_label(self) -> str:
        suffix = self.conversation_id[:12]
        detail_parts: list[str] = []
        if self.visible_message_count >= 0:
            detail_parts.append(f"{self.visible_message_count} visible")
        if self.latest_timestamp is not None:
            detail_parts.append(f"latest {_format_timestamp(self.latest_timestamp)}")
        preview = re.sub(r"\s+", " ", self.latest_preview).strip()
        if preview:
            detail_parts.append(f'“{preview[:80]}”')
        details = f" — {', '.join(detail_parts)}" if detail_parts else ""
        return f"{self.label}  ({suffix}){details}"


@dataclass(frozen=True)
class ExportReport:
    output_directory: Path
    index_file: Path
    message_count: int
    decrypted_attachments: int
    missing_attachments: int
    attachment_errors: int
    new_message_count: int = 0
    total_message_count: int = 0
    updated_existing: bool = False


def session_profile_from_environment(
    environment: Mapping[str, str] | None = None,
) -> SessionProfile:
    """Resolve the Windows Session profile, with a test/developer override."""
    env = os.environ if environment is None else environment
    override = env.get("SESSION_EXPORT_PROFILE", "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        appdata = env.get("APPDATA", "").strip()
        if not appdata:
            raise ExportError(
                "Windows APPDATA could not be found. This exporter must run in the "
                "Windows account that uses Session."
            )
        root = Path(appdata) / "Session"

    return SessionProfile(
        root=root,
        database=root / "sql" / "db.sqlite",
        config=root / "config.json",
        attachments=root / "attachments.noindex",
    )


def validate_profile(profile: SessionProfile) -> None:
    if not profile.root.is_dir():
        raise ExportError(f"Session data folder was not found: {profile.root}")
    if profile.root.is_symlink():
        raise ExportError("Refusing to use a symbolic-link Session data folder.")
    if not profile.database.is_file() or profile.database.is_symlink():
        raise ExportError(f"Session database was not found: {profile.database}")
    if not profile.config.is_file() or profile.config.is_symlink():
        raise ExportError(f"Session configuration was not found: {profile.config}")
    if not profile.attachments.is_dir() or profile.attachments.is_symlink():
        raise ExportError(f"Session attachments folder was not found: {profile.attachments}")


def _tasklist_image_names() -> set[str]:
    if os.name != "nt":
        return set()
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise ExportError("Windows could not check whether Session is running.") from exc
    if result.returncode != 0:
        raise ExportError("Windows could not check whether Session is running.")

    names: set[str] = set()
    for row in csv.reader(result.stdout.splitlines()):
        if row:
            names.add(row[0].strip().lower())
    return names


def session_is_running() -> bool:
    process_names = _tasklist_image_names()
    return bool({"session.exe", "session-desktop.exe"} & process_names)


def require_session_stopped() -> None:
    if session_is_running():
        raise ExportError(
            "Session is still running. Quit it from the system-tray icon, then try again."
        )


def load_database_key(config_path: Path) -> str:
    """Load the raw SQLCipher key without ever printing or persisting it."""
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError("Session config.json could not be read.") from exc

    key = payload.get("key") if isinstance(payload, dict) else None
    if not isinstance(key, str) or HEX_KEY_RE.fullmatch(key) is None:
        raise ExportError(
            "Session config.json does not contain the expected 64-character database key."
        )
    return key


def _load_sqlcipher_module():
    try:
        import sqlcipher3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ExportError(
            "The SQLCipher component is missing. Run Setup Windows.cmd, or use the "
            "packaged Windows executable."
        ) from exc
    return sqlcipher3


def prepare_plaintext_database(
    profile: SessionProfile,
    work_directory: Path,
    sqlcipher_module=None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Copy and decrypt the live database into an automatically cleaned work area."""
    report = progress or (lambda _message: None)
    report(f"Checking the Session data folder: {profile.root}")
    validate_profile(profile)
    report("The Session database, configuration, and attachment store were found.")
    report("Confirming that Session Desktop is fully closed...")
    require_session_stopped()
    work_directory.mkdir(parents=True, exist_ok=True)

    encrypted_copy = work_directory / "session-encrypted.sqlite"
    plaintext_database = work_directory / "session-plaintext.sqlite"
    if encrypted_copy.exists() or plaintext_database.exists():
        raise ExportError("The private working directory is not empty.")

    try:
        report("Copying the encrypted database into private temporary working storage...")
        shutil.copyfile(profile.database, encrypted_copy)
    except OSError as exc:
        raise ExportError("The Session database could not be copied to private working storage.") from exc

    # Recheck after the copy in case Session was reopened while the copy was running.
    report("The encrypted copy is complete. Checking once more that Session stayed closed...")
    require_session_stopped()
    report("Reading the database key from Session's configuration (the key is never displayed)...")
    key = load_database_key(profile.config)
    cipher = sqlcipher_module or _load_sqlcipher_module()
    connection = None
    attached = False
    try:
        report("Opening the private database copy with SQLCipher...")
        connection = cipher.connect(str(encrypted_copy))
        # The key has been strictly validated as 64 hex characters, so this SQL
        # contains no user-controlled syntax. It never enters process arguments.
        connection.execute(f"PRAGMA key = \"x'{key}'\";")
        count_row = connection.execute("SELECT count(*) FROM sqlite_master;").fetchone()
        if not count_row or int(count_row[0]) < 1:
            raise ExportError("The Session database key did not open the copied database.")

        report("The key opened the database successfully. Creating a temporary decrypted snapshot...")
        destination = str(plaintext_database).replace("'", "''")
        connection.execute(f"ATTACH DATABASE '{destination}' AS plaintext KEY '';")
        attached = True
        connection.execute("SELECT sqlcipher_export('plaintext');").fetchone()
        connection.execute("DETACH DATABASE plaintext;")
        attached = False
    except ExportError:
        raise
    except Exception:
        # Do not forward SQLCipher diagnostics; doing so risks exposing SQL input.
        raise ExportError("SQLCipher could not decrypt the copied Session database.") from None
    finally:
        key = ""  # Best effort only; Python strings cannot be reliably zeroized.
        if connection is not None:
            if attached:
                try:
                    connection.execute("DETACH DATABASE plaintext;")
                except Exception:
                    pass
            try:
                connection.close()
            except Exception:
                pass

    if not plaintext_database.is_file():
        raise ExportError("SQLCipher did not create the temporary plaintext database.")

    report("Checking the integrity of the temporary decrypted snapshot...")
    try:
        with sqlite3.connect(plaintext_database.as_uri() + "?mode=ro", uri=True) as database:
            result = database.execute("PRAGMA quick_check;").fetchone()
    except sqlite3.Error as exc:
        raise ExportError("The decrypted database could not be validated.") from exc
    if not result or result[0] != "ok":
        raise ExportError("The decrypted database failed its integrity check.")
    report("The decrypted snapshot passed its integrity check and is ready to read.")
    return plaintext_database


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def list_conversations(database_path: Path) -> list[Conversation]:
    try:
        with _connect_read_only(database_path) as connection:
            rows = connection.execute(
                """
                SELECT
                  c.id,
                  COALESCE(
                    NULLIF(trim(c.nickname), ''),
                    NULLIF(trim(c.displayNameInProfile), ''),
                    'private'
                  ) AS label,
                  (
                    SELECT count(*) FROM messages AS counted
                    WHERE counted.conversationId = c.id
                      AND CAST(coalesce(counted.isDeleted, '0') AS INTEGER) = 0
                  ) AS visible_message_count,
                  (
                    SELECT recent.sort_timestamp_full FROM messages AS recent
                    WHERE recent.conversationId = c.id
                      AND CAST(coalesce(recent.isDeleted, '0') AS INTEGER) = 0
                    ORDER BY recent.sort_timestamp_full DESC LIMIT 1
                  ) AS latest_timestamp,
                  COALESCE((
                    SELECT substr(replace(replace(recent.body, char(13), ' '), char(10), ' '), 1, 80)
                    FROM messages AS recent
                    WHERE recent.conversationId = c.id
                      AND CAST(coalesce(recent.isDeleted, '0') AS INTEGER) = 0
                      AND length(trim(coalesce(recent.body, ''))) > 0
                    ORDER BY recent.sort_timestamp_full DESC LIMIT 1
                  ), '') AS latest_preview
                FROM conversations AS c
                WHERE c.id IS NOT NULL AND length(trim(c.id)) > 0
                ORDER BY label COLLATE NOCASE, id;
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise ExportError("Conversation names could not be read from the decrypted database.") from exc
    conversations = [
        Conversation(
            str(row["id"]),
            str(row["label"]),
            int(row["visible_message_count"] or 0),
            row["latest_timestamp"],
            str(row["latest_preview"] or ""),
        )
        for row in rows
    ]
    if not conversations:
        raise ExportError("No conversations were found in the Session database.")
    return conversations


def _load_attachment_key(connection: sqlite3.Connection) -> bytes:
    cursor = connection.cursor()
    value: str | None = None
    try:
        row = cursor.execute(
            "SELECT json FROM items WHERE id = 'local_attachment_encrypted_key' LIMIT 1;"
        ).fetchone()
        if row:
            payload = json.loads(row["json"])
            candidate = payload.get("value") if isinstance(payload, dict) else None
            if isinstance(candidate, str):
                value = candidate.strip()
    except (sqlite3.Error, json.JSONDecodeError, TypeError):
        value = None

    if value is None:
        try:
            rows = cursor.execute("SELECT json FROM items;").fetchall()
        except sqlite3.Error as exc:
            raise ExportError("The local attachment key could not be read.") from exc
        for row in rows:
            try:
                payload = json.loads(row["json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("id") == "local_attachment_encrypted_key":
                candidate = payload.get("value")
                if isinstance(candidate, str):
                    value = candidate.strip()
                    break

    if value is None or HEX_KEY_RE.fullmatch(value) is None:
        raise ExportError("The local attachment key was not found in the Session database.")
    try:
        return binascii.unhexlify(value)
    except (binascii.Error, ValueError) as exc:
        raise ExportError("The local attachment key is invalid.") from exc


def decrypt_secretstream_bytes(data: bytes, key: bytes) -> bytes:
    try:
        from nacl.bindings import (  # type: ignore[import-not-found]
            crypto_secretstream_xchacha20poly1305_HEADERBYTES,
            crypto_secretstream_xchacha20poly1305_KEYBYTES,
            crypto_secretstream_xchacha20poly1305_init_pull,
            crypto_secretstream_xchacha20poly1305_pull,
            crypto_secretstream_xchacha20poly1305_state,
        )
    except ImportError as exc:
        raise ExportError(
            "The attachment decryption component is missing. Run Setup Windows.cmd, "
            "or use the packaged Windows executable."
        ) from exc

    if len(key) != crypto_secretstream_xchacha20poly1305_KEYBYTES:
        raise ExportError("The local attachment key has an unexpected length.")
    header_length = crypto_secretstream_xchacha20poly1305_HEADERBYTES
    if len(data) <= header_length:
        raise ExportError("An encrypted attachment is too short.")
    state = crypto_secretstream_xchacha20poly1305_state()
    crypto_secretstream_xchacha20poly1305_init_pull(state, data[:header_length], key)
    message, _tag = crypto_secretstream_xchacha20poly1305_pull(
        state, data[header_length:], None
    )
    return message


def _safe_filename(value: str, fallback: str = "file") -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "").strip())
    name = re.sub(r"_+", "_", name).strip(" ._")
    if not name:
        name = fallback
    stem = name.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name[:180].rstrip(" .") or fallback


def _extension_for(mime_type: str, original_name: str) -> str:
    mime = (mime_type or "").strip().lower()
    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]
    suffix = Path(original_name).suffix if original_name else ""
    if suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
        return suffix.lower()
    return guess_extension(mime) or "" if mime else ""


def _resolve_attachment(attachment_root: Path, stored_path: str) -> Path:
    root = attachment_root.resolve()
    relative = Path(stored_path)
    if relative.is_absolute():
        raise ExportError("An attachment path points outside Session's attachment store.")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ExportError("An attachment path points outside Session's attachment store.") from exc
    return candidate


def _unique_destination(directory: Path, requested_name: str) -> Path:
    candidate = directory / requested_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _html_page(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>
body {{ font-family: system-ui, "Segoe UI", sans-serif; margin: 24px; color: #171717; }}
.meta {{ color: #555; margin-bottom: 18px; }}
.msg {{ border-bottom: 1px solid #e5e5e5; padding: 10px 0; }}
.hdr {{ font-size: 13px; color: #666; margin-bottom: 6px; }}
.speaker {{ font-weight: 700; color: #111; }}
.text {{ white-space: pre-wrap; line-height: 1.4; }}
.attachments {{ margin-top: 8px; display: grid; gap: 10px; }}
.att {{ padding: 10px; border: 1px solid #e5e5e5; border-radius: 10px; }}
.small {{ font-size: 12px; color: #777; }}
img {{ max-width: min(900px, 100%); height: auto; border-radius: 10px; display: block; }}
video, audio {{ width: min(900px, 100%); }}
a {{ word-break: break-all; }}
</style>
</head>
<body>
{content}
</body>
</html>
"""


def _format_timestamp(value) -> str:
    try:
        timestamp = int(value)
        return datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "Unknown time"


ARCHIVE_FILENAME = "archive.json"
ARCHIVE_VERSION = 1


def _message_archive_key(message: sqlite3.Row, conversation_id: str) -> str:
    message_id = str(message["id"] or "").strip()
    if message_id:
        return f"id:{message_id}"
    fallback = json.dumps(
        {
            "conversation": conversation_id,
            "type": str(message["type"] or ""),
            "timestamp": str(message["sort_timestamp_full"] or ""),
            "body": str(message["body"] or ""),
            "json": str(message["json"] or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "fallback:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def _attachment_archive_key(attachment: dict, position: int) -> str:
    attachment_id = str(attachment.get("id") or "").strip()
    if attachment_id:
        return f"id:{attachment_id}"
    identity = json.dumps(
        {
            "path": str(attachment.get("path") or ""),
            "fileName": str(attachment.get("fileName") or ""),
            "contentType": str(attachment.get("contentType") or ""),
            "position": position,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "fallback:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _stable_output_directory(output_parent: Path, conversation: Conversation) -> Path:
    # The app intentionally maintains one obvious, reusable folder beside the EXE.
    # The archive metadata prevents a different conversation from being merged into it.
    return output_parent / "session-export"


def _load_archive(archive_file: Path, conversation: Conversation) -> dict:
    try:
        archive = json.loads(archive_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError(
            "The existing archive index could not be read. The old export was left unchanged."
        ) from exc
    if not isinstance(archive, dict) or archive.get("version") != ARCHIVE_VERSION:
        raise ExportError("The existing export uses an unsupported archive format.")
    if archive.get("conversation_id") != conversation.conversation_id:
        raise ExportError("The existing export belongs to a different conversation.")
    if not isinstance(archive.get("messages"), list):
        raise ExportError("The existing export's message index is damaged.")
    if not isinstance(archive.get("captures"), list):
        archive["captures"] = []
    return archive


def _write_text_atomic(destination: Path, text: str) -> None:
    temporary = destination.with_name(destination.name + ".new")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ExportError(f"Could not safely write {destination.name}.") from exc


def _valid_exported_attachment(output_directory: Path, relative_name: object) -> Path | None:
    if not isinstance(relative_name, str) or not relative_name.startswith("attachments/"):
        return None
    candidate = (output_directory / Path(relative_name)).resolve()
    try:
        candidate.relative_to(output_directory.resolve())
    except ValueError:
        return None
    return candidate


def _render_archive_html(archive: dict, output_directory: Path) -> str:
    label = str(archive.get("label") or "Session conversation")
    messages = archive.get("messages", [])
    message_blocks: list[str] = []
    attachment_total = 0

    def message_sort_key(message: dict) -> tuple[int, str, str]:
        value = message.get("timestamp")
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            numeric = 0
        return numeric, str(message.get("id") or ""), str(message.get("archive_key") or "")

    for message in sorted(messages, key=message_sort_key):
        direction = str(message.get("type") or "")
        speaker = "Me" if direction == "outgoing" else label
        timestamp_text = _format_timestamp(message.get("timestamp"))
        body = str(message.get("body") or "").replace("\r", "")
        body_html = f'<div class="text">{html.escape(body)}</div>' if body.strip() else ""
        attachment_cards: list[str] = []
        for attachment in message.get("attachments", []):
            if not isinstance(attachment, dict):
                continue
            relative_url = attachment.get("exported_name")
            exported_file = _valid_exported_attachment(output_directory, relative_url)
            if exported_file is None or not exported_file.is_file():
                continue
            attachment_total += 1
            mime_type = str(attachment.get("mime_type") or "").lower()
            attachment_id = str(attachment.get("attachment_id") or "")
            metadata = ", ".join(
                item for item in (mime_type, f"id={attachment_id}" if attachment_id else "") if item
            )
            escaped_url = html.escape(str(relative_url).replace("\\", "/"), quote=True)
            escaped_name = html.escape(exported_file.name)
            if mime_type in INLINE_IMAGE:
                media = f'<img src="{escaped_url}" loading="lazy"/>'
            elif mime_type in INLINE_VIDEO:
                media = f'<video controls preload="metadata" src="{escaped_url}"></video>'
            elif mime_type in INLINE_AUDIO:
                media = f'<audio controls preload="metadata" src="{escaped_url}"></audio>'
            else:
                media = ""
            attachment_cards.append(
                '<div class="att">'
                f'<div class="small">{html.escape(metadata)}</div>{media}'
                f'<div><a href="{escaped_url}" download>{escaped_name}</a></div>'
                "</div>"
            )
        attachments_html = (
            f'<div class="attachments">{"".join(attachment_cards)}</div>'
            if attachment_cards
            else ""
        )
        if body_html or attachments_html:
            message_blocks.append(
                '<div class="msg">'
                f'<div class="hdr"><span class="speaker">{html.escape(speaker)}</span>'
                f' &middot; {html.escape(timestamp_text)} &middot; {html.escape(direction)}</div>'
                f"{body_html}{attachments_html}</div>"
            )

    captures = archive.get("captures", [])
    last_capture = captures[-1] if captures else {}
    updated_at = html.escape(str(last_capture.get("captured_at") or "Unknown"))
    header = (
        f"<h1>{html.escape(label)}</h1>"
        '<div class="meta">'
        f"<div><strong>Conversation ID:</strong> {html.escape(str(archive.get('conversation_id') or ''))}</div>"
        f"<div><strong>Archived messages:</strong> {len(messages)} &middot; "
        f"<strong>Attachments:</strong> {attachment_total} &middot; "
        f"<strong>Captures:</strong> {len(captures)}</div>"
        f"<div><strong>Last updated:</strong> {updated_at}</div>"
        "<div>This page accumulates each capture in chronological order. Earlier messages remain "
        "here even after disappearing from Session.</div></div>"
    )
    return _html_page(label, header + "".join(message_blocks))


def export_conversation(
    database_path: Path,
    attachment_root: Path,
    conversation: Conversation,
    output_parent: Path,
    attachment_decryptor: Callable[[bytes, bytes], bytes] = decrypt_secretstream_bytes,
    progress: Callable[[str], None] | None = None,
) -> ExportReport:
    """Create or incrementally update one durable HTML conversation archive."""
    report_progress = progress or (lambda _message: None)
    output_parent = output_parent.expanduser().resolve()
    if not output_parent.is_dir():
        raise ExportError("The selected export destination is not a folder.")

    output_directory = _stable_output_directory(output_parent, conversation)
    archive_file = output_directory / ARCHIVE_FILENAME
    created_directory = False
    if output_directory.exists():
        if not output_directory.is_dir() or not archive_file.is_file():
            raise ExportError(
                f"The intended export folder already exists but is not a compatible archive: "
                f"{output_directory}"
            )
        report_progress(f"Found the existing archive: {output_directory}")
        report_progress("Loading its message index so this run can add only new material...")
        archive = _load_archive(archive_file, conversation)
        updated_existing = True
    else:
        output_directory.mkdir(parents=False, exist_ok=False)
        created_directory = True
        updated_existing = False
        archive = {
            "version": ARCHIVE_VERSION,
            "conversation_id": conversation.conversation_id,
            "label": conversation.label,
            "messages": [],
            "captures": [],
        }
        report_progress(f"Created a permanent archive folder: {output_directory}")

    try:
        report_progress("Opening the temporary decrypted database snapshot in read-only mode...")
        with _connect_read_only(database_path) as connection:
            row = connection.execute(
                """
                SELECT
                  id,
                  COALESCE(
                    NULLIF(trim(nickname), ''),
                    NULLIF(trim(displayNameInProfile), ''),
                    'private'
                  ) AS label
                FROM conversations
                WHERE id = ?
                LIMIT 1;
                """,
                (conversation.conversation_id,),
            ).fetchone()
            if row is None:
                raise ExportError("The selected conversation no longer exists in the snapshot.")

            report_progress("The selected conversation was found. Reading the local attachment key...")
            attachment_key = _load_attachment_key(connection)
            messages = connection.execute(
                """
                SELECT id, type, sort_timestamp_full, body, json
                FROM messages
                WHERE conversationId = ?
                  AND CAST(coalesce(isDeleted, '0') AS INTEGER) = 0
                ORDER BY sort_timestamp_full ASC;
                """,
                (conversation.conversation_id,),
            ).fetchall()
            report_progress(
                f"The current Session snapshot contains {len(messages)} non-deleted message(s) "
                "for this chat."
            )

            archive["label"] = conversation.label
            archived_messages = archive["messages"]
            message_by_key = {
                str(item.get("archive_key")): item
                for item in archived_messages
                if isinstance(item, dict) and item.get("archive_key")
            }
            new_message_count = 0
            decrypted_count = 0
            missing_count = 0
            decrypt_errors = 0
            attachments_directory = output_directory / "attachments"

            for message in messages:
                direction = str(message["type"] or "")
                body = str(message["body"] or "").replace("\r", "")
                message_key = _message_archive_key(message, conversation.conversation_id)
                archived_message = message_by_key.get(message_key)
                if archived_message is None:
                    archived_message = {
                        "archive_key": message_key,
                        "id": str(message["id"] or ""),
                        "type": direction,
                        "timestamp": message["sort_timestamp_full"],
                        "body": body,
                        "attachments": [],
                    }
                    archived_messages.append(archived_message)
                    message_by_key[message_key] = archived_message
                    new_message_count += 1
                else:
                    archived_message["type"] = direction
                    archived_message["timestamp"] = message["sort_timestamp_full"]
                    if body or not archived_message.get("body"):
                        archived_message["body"] = body

                try:
                    message_json = json.loads(message["json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    message_json = {}
                attachments = (
                    message_json.get("attachments", [])
                    if isinstance(message_json, dict)
                    else []
                )
                archived_attachments = archived_message.setdefault("attachments", [])
                attachment_by_key = {
                    str(item.get("archive_key")): item
                    for item in archived_attachments
                    if isinstance(item, dict) and item.get("archive_key")
                }

                for position, attachment in enumerate(attachments):
                    if not isinstance(attachment, dict):
                        continue
                    stored_path = str(attachment.get("path") or "").strip()
                    if not stored_path:
                        continue
                    attachment_archive_key = _attachment_archive_key(attachment, position)
                    archived_attachment = attachment_by_key.get(attachment_archive_key)
                    if archived_attachment is None:
                        archived_attachment = {"archive_key": attachment_archive_key}
                        archived_attachments.append(archived_attachment)
                        attachment_by_key[attachment_archive_key] = archived_attachment

                    mime_type = str(attachment.get("contentType") or "").strip().lower()
                    original_name = str(attachment.get("fileName") or "").strip()
                    attachment_id = str(attachment.get("id") or "").strip()
                    archived_attachment.update(
                        {
                            "stored_path": stored_path,
                            "original_name": original_name,
                            "mime_type": mime_type,
                            "attachment_id": attachment_id,
                        }
                    )
                    existing_file = _valid_exported_attachment(
                        output_directory, archived_attachment.get("exported_name")
                    )
                    if existing_file is not None and existing_file.is_file():
                        continue
                    try:
                        source = _resolve_attachment(attachment_root, stored_path)
                    except ExportError:
                        decrypt_errors += 1
                        continue
                    if not source.is_file():
                        missing_count += 1
                        report_progress(
                            f"Attachment is no longer present in Session's store: {original_name or attachment_id or stored_path}"
                        )
                        continue

                    extension = _extension_for(mime_type, original_name)
                    if original_name:
                        requested_name = _safe_filename(original_name)
                        if extension and not Path(requested_name).suffix:
                            requested_name += extension
                    else:
                        stamp = _safe_filename(str(message["sort_timestamp_full"] or "unknown"))
                        id_part = _safe_filename(attachment_id, "attachment")
                        requested_name = f"{stamp}_{direction}_{id_part}{extension}"

                    attachments_directory.mkdir(parents=True, exist_ok=True)
                    destination = _unique_destination(attachments_directory, requested_name)
                    report_progress(f"Decrypting attachment: {requested_name}")
                    try:
                        plaintext = attachment_decryptor(source.read_bytes(), attachment_key)
                        destination.write_bytes(plaintext)
                    except Exception:
                        decrypt_errors += 1
                        try:
                            destination.unlink(missing_ok=True)
                        except OSError:
                            pass
                        report_progress(f"Could not decrypt attachment: {requested_name}")
                        continue

                    decrypted_count += 1
                    archived_attachment["exported_name"] = f"attachments/{destination.name}"

        archive["captures"].append(
            {
                "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "snapshot_messages": len(messages),
                "new_messages": new_message_count,
                "new_attachments": decrypted_count,
                "missing_attachments": missing_count,
                "attachment_errors": decrypt_errors,
            }
        )
        report_progress(
            f"Merge complete: {new_message_count} new message(s), {decrypted_count} new attachment(s), "
            f"{len(archive['messages'])} message(s) retained in total."
        )
        report_progress("Writing the durable archive index safely...")
        _write_text_atomic(
            archive_file,
            json.dumps(archive, ensure_ascii=False, indent=2),
        )
        index_file = output_directory / "index.html"
        report_progress("Rebuilding index.html as one chronological, accumulating conversation...")
        _write_text_atomic(index_file, _render_archive_html(archive, output_directory))
        report_progress("The HTML archive is complete and ready to open.")
        return ExportReport(
            output_directory=output_directory,
            index_file=index_file,
            message_count=len(messages),
            decrypted_attachments=decrypted_count,
            missing_attachments=missing_count,
            attachment_errors=decrypt_errors,
            new_message_count=new_message_count,
            total_message_count=len(archive["messages"]),
            updated_existing=updated_existing,
        )
    except ExportError:
        if created_directory:
            shutil.rmtree(output_directory, ignore_errors=True)
        raise
    except Exception as exc:
        if created_directory:
            shutil.rmtree(output_directory, ignore_errors=True)
        raise ExportError("The selected conversation could not be exported.") from exc


def default_documents_directory() -> Path:
    # A folder picker is always shown, so this is only its initial location.
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()


def default_export_directory() -> Path:
    """Default beside the packaged EXE, with a Downloads fallback for source runs."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else default_documents_directory()
