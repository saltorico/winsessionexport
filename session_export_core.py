"""Windows-safe core for exporting one Session Desktop conversation.

The GUI imports this module.  Cryptographic dependencies are imported lazily so
the non-cryptographic unit tests can run on any development machine.
"""

from __future__ import annotations

import binascii
import csv
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

    @property
    def display_label(self) -> str:
        suffix = self.conversation_id[:12]
        return f"{self.label}  ({suffix})"


@dataclass(frozen=True)
class ExportReport:
    output_directory: Path
    index_file: Path
    message_count: int
    decrypted_attachments: int
    missing_attachments: int
    attachment_errors: int


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
) -> Path:
    """Copy and decrypt the live database into an automatically cleaned work area."""
    validate_profile(profile)
    require_session_stopped()
    work_directory.mkdir(parents=True, exist_ok=True)

    encrypted_copy = work_directory / "session-encrypted.sqlite"
    plaintext_database = work_directory / "session-plaintext.sqlite"
    if encrypted_copy.exists() or plaintext_database.exists():
        raise ExportError("The private working directory is not empty.")

    try:
        shutil.copyfile(profile.database, encrypted_copy)
    except OSError as exc:
        raise ExportError("The Session database could not be copied to private working storage.") from exc

    # Recheck after the copy in case Session was reopened while the copy was running.
    require_session_stopped()
    key = load_database_key(profile.config)
    cipher = sqlcipher_module or _load_sqlcipher_module()
    connection = None
    attached = False
    try:
        connection = cipher.connect(str(encrypted_copy))
        # The key has been strictly validated as 64 hex characters, so this SQL
        # contains no user-controlled syntax. It never enters process arguments.
        connection.execute(f"PRAGMA key = \"x'{key}'\";")
        count_row = connection.execute("SELECT count(*) FROM sqlite_master;").fetchone()
        if not count_row or int(count_row[0]) < 1:
            raise ExportError("The Session database key did not open the copied database.")

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

    try:
        with sqlite3.connect(plaintext_database.as_uri() + "?mode=ro", uri=True) as database:
            result = database.execute("PRAGMA quick_check;").fetchone()
    except sqlite3.Error as exc:
        raise ExportError("The decrypted database could not be validated.") from exc
    if not result or result[0] != "ok":
        raise ExportError("The decrypted database failed its integrity check.")
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
                  id,
                  COALESCE(
                    NULLIF(trim(nickname), ''),
                    NULLIF(trim(displayNameInProfile), ''),
                    'private'
                  ) AS label
                FROM conversations
                WHERE id IS NOT NULL AND length(trim(id)) > 0
                ORDER BY label COLLATE NOCASE, id;
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise ExportError("Conversation names could not be read from the decrypted database.") from exc
    conversations = [Conversation(str(row["id"]), str(row["label"])) for row in rows]
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


def export_conversation(
    database_path: Path,
    attachment_root: Path,
    conversation: Conversation,
    output_parent: Path,
    attachment_decryptor: Callable[[bytes, bytes], bytes] = decrypt_secretstream_bytes,
) -> ExportReport:
    """Export one exact conversation to a new, non-overwriting HTML directory."""
    output_parent = output_parent.expanduser().resolve()
    if not output_parent.is_dir():
        raise ExportError("The selected export destination is not a folder.")

    folder_label = _safe_filename(conversation.label, "conversation")[:80]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_directory = output_parent / f"SessionChat_{folder_label}_{timestamp}"
    output_directory.mkdir(parents=False, exist_ok=False)

    try:
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

            message_blocks: list[str] = []
            decrypted_count = 0
            missing_count = 0
            decrypt_errors = 0
            attachments_directory = output_directory / "attachments"

            for message in messages:
                direction = str(message["type"] or "")
                speaker = "Me" if direction == "outgoing" else conversation.label
                timestamp_text = _format_timestamp(message["sort_timestamp_full"])
                body = str(message["body"] or "").replace("\r", "")
                body_html = (
                    f'<div class="text">{html.escape(body)}</div>' if body.strip() else ""
                )

                try:
                    message_json = json.loads(message["json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    message_json = {}
                attachments = (
                    message_json.get("attachments", [])
                    if isinstance(message_json, dict)
                    else []
                )
                attachment_cards: list[str] = []

                for attachment in attachments:
                    if not isinstance(attachment, dict):
                        continue
                    stored_path = str(attachment.get("path") or "").strip()
                    if not stored_path:
                        continue
                    try:
                        source = _resolve_attachment(attachment_root, stored_path)
                    except ExportError:
                        decrypt_errors += 1
                        continue
                    if not source.is_file():
                        missing_count += 1
                        continue

                    mime_type = str(attachment.get("contentType") or "").strip().lower()
                    original_name = str(attachment.get("fileName") or "").strip()
                    attachment_id = str(attachment.get("id") or "").strip()
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
                    try:
                        plaintext = attachment_decryptor(source.read_bytes(), attachment_key)
                        destination.write_bytes(plaintext)
                    except Exception:
                        decrypt_errors += 1
                        try:
                            destination.unlink(missing_ok=True)
                        except OSError:
                            pass
                        continue

                    decrypted_count += 1
                    relative_url = f"attachments/{destination.name}"
                    metadata = ", ".join(
                        item
                        for item in (
                            mime_type,
                            f"id={attachment_id}" if attachment_id else "",
                        )
                        if item
                    )
                    escaped_url = html.escape(relative_url, quote=True)
                    escaped_name = html.escape(destination.name)
                    escaped_meta = html.escape(metadata)
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
                        f'<div class="small">{escaped_meta}</div>{media}'
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

        header = (
            f"<h1>{html.escape(conversation.label)}</h1>"
            '<div class="meta">'
            f"<div><strong>Conversation ID:</strong> {html.escape(conversation.conversation_id)}</div>"
            f"<div><strong>Messages:</strong> {len(messages)} &middot; "
            f"<strong>Decrypted attachments:</strong> {decrypted_count} &middot; "
            f"<strong>Missing:</strong> {missing_count} &middot; "
            f"<strong>Decrypt errors:</strong> {decrypt_errors}</div></div>"
        )
        index_file = output_directory / "index.html"
        index_file.write_text(
            _html_page(conversation.label, header + "".join(message_blocks)),
            encoding="utf-8",
        )
        return ExportReport(
            output_directory=output_directory,
            index_file=index_file,
            message_count=len(messages),
            decrypted_attachments=decrypted_count,
            missing_attachments=missing_count,
            attachment_errors=decrypt_errors,
        )
    except ExportError:
        shutil.rmtree(output_directory, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(output_directory, ignore_errors=True)
        raise ExportError("The selected conversation could not be exported.") from exc


def default_documents_directory() -> Path:
    # A folder picker is always shown, so this is only its initial location.
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()

