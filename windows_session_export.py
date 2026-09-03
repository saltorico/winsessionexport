"""Guided Tkinter front end for the Windows Session Chat Exporter."""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import traceback
import webbrowser
from pathlib import Path
from tkinter import (
    BOTH, END, LEFT, RIGHT, VERTICAL, X, BooleanVar, Button, Checkbutton,
    Entry, Frame, Label, Listbox, Scrollbar, StringVar, Text, Tk,
)
from tkinter import filedialog, messagebox

from session_export_core import (
    Conversation,
    ExportError,
    default_export_directory,
    export_conversation,
    list_conversations,
    prepare_plaintext_database,
    require_session_stopped,
    session_profile_from_environment,
    validate_profile,
)


def _settings_file() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "SessionChatExporter" / "settings.json"


class SessionExporterApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("Session Chat Exporter")
        self.root.geometry("880x760")
        self.root.minsize(700, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.status = StringVar(value="Ready. First, fully quit Session from its system-tray icon.")
        self.destination = StringVar(value=str(default_export_directory()))
        self.search_text = StringVar()
        self.remember_chat = BooleanVar(value=True)
        self.conversations: list[Conversation] = []
        self.visible_conversations: list[Conversation] = []
        self.work_area: tempfile.TemporaryDirectory[str] | None = None
        self.plaintext_database: Path | None = None
        self.profile = None
        self.settings = self._load_settings()

        Label(
            root,
            text="Build one continuing archive of a Session conversation",
            font=("Segoe UI", 16, "bold"),
            anchor="w",
        ).pack(fill=X, padx=18, pady=(16, 4))
        Label(
            root,
            text=(
                "This guide makes a private database snapshot, helps you find the right chat, "
                "then creates session-export\\index.html. Run it again later to append newly "
                "visible messages—even when earlier 12-hour messages have already disappeared."
            ),
            justify=LEFT,
            anchor="w",
            wraplength=835,
        ).pack(fill=X, padx=18, pady=(0, 10))

        quick_frame = Frame(root, relief="groove", borderwidth=1)
        quick_frame.pack(fill=X, padx=18, pady=(0, 10))
        self.quick_button = Button(
            quick_frame,
            text="Next time: update my saved chat now",
            command=self.quick_update,
            padx=12,
            pady=6,
            state="normal" if self._saved_conversation() else "disabled",
        )
        self.quick_button.pack(side=LEFT, padx=8, pady=8)
        self.quick_label = Label(
            quick_frame,
            text=self._quick_description(),
            anchor="w",
            justify=LEFT,
            wraplength=500,
        )
        self.quick_label.pack(side=LEFT, fill=X, expand=True, padx=(4, 8), pady=8)

        controls = Frame(root)
        controls.pack(fill=X, padx=18)
        self.load_button = Button(
            controls,
            text="1. Find my Session chats",
            command=self.load_conversations,
            padx=12,
            pady=6,
        )
        self.load_button.pack(side=LEFT)
        self.export_button = Button(
            controls,
            text="3. Export / update selected chat",
            command=self.export_selected,
            padx=12,
            pady=6,
            state="disabled",
        )
        self.export_button.pack(side=LEFT, padx=(10, 0))

        search_frame = Frame(root)
        search_frame.pack(fill=X, padx=18, pady=(10, 4))
        Label(search_frame, text="2. Find the right chat:", anchor="w").pack(side=LEFT)
        search_entry = Entry(search_frame, textvariable=self.search_text)
        search_entry.pack(side=LEFT, fill=X, expand=True, padx=(8, 0))
        self.search_text.trace_add("write", lambda *_args: self._apply_filter())

        list_frame = Frame(root)
        list_frame.pack(fill=BOTH, expand=True, padx=18, pady=(0, 8))
        scrollbar = Scrollbar(list_frame, orient=VERTICAL)
        scrollbar.pack(side=RIGHT, fill="y")
        self.listbox = Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            font=("Segoe UI", 10),
            activestyle="dotbox",
            exportselection=False,
            height=8,
        )
        self.listbox.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.config(command=self.listbox.yview)
        self.listbox.bind("<<ListboxSelect>>", self._selection_changed)
        self.listbox.bind("<Double-Button-1>", lambda _event: self.export_selected())

        destination_frame = Frame(root)
        destination_frame.pack(fill=X, padx=18, pady=(0, 4))
        Label(destination_frame, text="Place session-export inside:", anchor="w").pack(side=LEFT)
        Entry(destination_frame, textvariable=self.destination).pack(
            side=LEFT, fill=X, expand=True, padx=8
        )
        self.browse_button = Button(destination_frame, text="Browse...", command=self.browse)
        self.browse_button.pack(side=RIGHT)
        Checkbutton(
            root,
            text='Remember this chat and enable the “Next time” quick-update button',
            variable=self.remember_chat,
            anchor="w",
        ).pack(fill=X, padx=18, pady=(0, 8))

        log_header = Frame(root)
        log_header.pack(fill=X, padx=18)
        Label(log_header, text="What the exporter is doing", font=("Segoe UI", 10, "bold")).pack(
            side=LEFT
        )
        Button(log_header, text="Copy log to clipboard", command=self.copy_log).pack(side=RIGHT)

        log_frame = Frame(root)
        log_frame.pack(fill=BOTH, expand=True, padx=18, pady=(4, 10))
        log_scroll = Scrollbar(log_frame, orient=VERTICAL)
        log_scroll.pack(side=RIGHT, fill="y")
        self.log = Text(
            log_frame,
            height=10,
            wrap="word",
            yscrollcommand=log_scroll.set,
            font=("Consolas", 9),
            state="disabled",
        )
        self.log.pack(side=LEFT, fill=BOTH, expand=True)
        log_scroll.config(command=self.log.yview)

        Label(root, textvariable=self.status, anchor="w", relief="sunken").pack(
            fill=X, side="bottom"
        )
        self._append_log("Session Chat Exporter started.")
        self._append_log(f"Program location: {Path(sys.executable).resolve()}")
        self._append_log(f"Windows/Python runtime: {platform.platform()} / {platform.python_version()}")
        self._append_log(
            f"Default result: {Path(self.destination.get()) / 'session-export' / 'index.html'}"
        )
        if self._saved_conversation():
            self._append_log("A previous chat is remembered. You can use the quick-update button.")

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(END, message.rstrip() + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")
        self.status.set(message.rstrip())
        self.root.update_idletasks()

    def copy_log(self) -> None:
        try:
            contents = self.log.get("1.0", END).rstrip()
            self.root.clipboard_clear()
            self.root.clipboard_append(contents)
            self.root.update()
            self.status.set("The diagnostic log was copied to the clipboard.")
        except Exception as exc:
            messagebox.showerror("Could not copy", str(exc), parent=self.root)

    def _load_settings(self) -> dict:
        try:
            payload = json.loads(_settings_file().read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _save_settings(self, conversation: Conversation, output_parent: Path) -> None:
        if not self.remember_chat.get():
            return
        payload = {
            "version": 1,
            "conversation_id": conversation.conversation_id,
            "label": conversation.label,
            "output_parent": str(output_parent),
        }
        destination = _settings_file()
        temporary = destination.with_name(destination.name + ".new")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, destination)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self._append_log(
                f"WARNING: The archive succeeded, but the quick-update setting could not be saved: {exc}"
            )
            return
        self.settings = payload
        self.quick_button.configure(state="normal")
        self.quick_label.configure(text=self._quick_description())
        self._append_log("Remembered this chat for the next quick update.")

    def _saved_conversation(self) -> Conversation | None:
        conversation_id = self.settings.get("conversation_id")
        label = self.settings.get("label")
        if isinstance(conversation_id, str) and conversation_id and isinstance(label, str):
            return Conversation(conversation_id, label or "private")
        return None

    def _quick_description(self) -> str:
        conversation = self._saved_conversation()
        if conversation is None:
            return "Available after the first successful export."
        return f"Remembered chat: {conversation.display_label}"

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.load_button.configure(state=state)
        self.browse_button.configure(state=state)
        self.quick_button.configure(
            state="disabled" if busy or self._saved_conversation() is None else "normal"
        )
        self.export_button.configure(
            state="disabled" if busy or self.plaintext_database is None else "normal"
        )
        self.root.update_idletasks()

    def _clean_work_area(self) -> None:
        self.plaintext_database = None
        if self.work_area is not None:
            self._append_log("Removing the temporary decrypted database snapshot...")
            self.work_area.cleanup()
            self.work_area = None

    def browse(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            title="Choose the folder that will contain session-export",
            initialdir=self.destination.get() or str(default_export_directory()),
            mustexist=True,
        )
        if selected:
            self.destination.set(selected)
            self._append_log(f"The archive will be placed in: {Path(selected) / 'session-export'}")

    def _prepare_snapshot(self) -> None:
        self._clean_work_area()
        self._append_log("Checking whether Session Desktop is still running...")
        require_session_stopped()
        self.profile = session_profile_from_environment()
        self._append_log(f"Looking for this Windows account's Session profile: {self.profile.root}")
        validate_profile(self.profile)
        self.work_area = tempfile.TemporaryDirectory(prefix="SessionChatExporter-")
        self.plaintext_database = prepare_plaintext_database(
            self.profile,
            Path(self.work_area.name),
            progress=self._append_log,
        )

    def load_conversations(self) -> None:
        self.conversations = []
        self.visible_conversations = []
        self.listbox.delete(0, END)
        self._set_busy(True)
        self._append_log("--- Starting the guided chat-selection process ---")
        try:
            self._prepare_snapshot()
            self._append_log("Reading conversation names from the decrypted snapshot...")
            self.conversations = list_conversations(self.plaintext_database)
            self._append_log(f"Found {len(self.conversations)} conversation(s).")
            self._apply_filter()
            self._set_busy(False)
            self._append_log("Select the intended chat, then use the export/update button.")
        except ExportError as exc:
            self._handle_error("Could not load conversations", exc)
        except Exception as exc:
            self._handle_error("Unexpected error while loading conversations", exc, unexpected=True)

    def _apply_filter(self) -> None:
        if not hasattr(self, "listbox"):
            return
        query = self.search_text.get().strip().casefold()
        self.visible_conversations = [
            conversation
            for conversation in self.conversations
            if not query
            or query in conversation.label.casefold()
            or query in conversation.conversation_id.casefold()
        ]
        self.listbox.delete(0, END)
        for conversation in self.visible_conversations:
            self.listbox.insert(END, conversation.display_label)
        if self.visible_conversations:
            self.listbox.selection_set(0)
            self.listbox.activate(0)
        self.export_button.configure(
            state="normal" if self.visible_conversations and self.plaintext_database else "disabled"
        )

    def _selection_changed(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if selection and self.visible_conversations:
            conversation = self.visible_conversations[int(selection[0])]
            self.status.set(f"Selected: {conversation.display_label}")

    def _output_parent(self, saved: bool = False) -> Path:
        raw = self.settings.get("output_parent") if saved else self.destination.get()
        output_parent = Path(str(raw or "")).expanduser().resolve()
        if not output_parent.is_dir():
            raise ExportError(f"The chosen parent folder does not exist: {output_parent}")
        return output_parent

    def _run_export(self, conversation: Conversation, output_parent: Path) -> None:
        if self.plaintext_database is None or self.profile is None:
            raise ExportError("The temporary database snapshot is not ready.")
        self._append_log(f"Selected conversation: {conversation.display_label}")
        self._append_log(f"Permanent archive folder: {output_parent / 'session-export'}")
        report = export_conversation(
            self.plaintext_database,
            self.profile.attachments,
            conversation,
            output_parent,
            progress=self._append_log,
        )
        self._save_settings(conversation, output_parent)
        action = "Updated" if report.updated_existing else "Created"
        self._append_log(
            f"{action} archive successfully: {report.new_message_count} new message(s), "
            f"{report.decrypted_attachments} new attachment(s), "
            f"{report.total_message_count} archived message(s) in total."
        )
        self._clean_work_area()
        detail = (
            f"{action} the continuing archive.\n\n"
            f"Messages visible in this Session snapshot: {report.message_count}\n"
            f"New messages added: {report.new_message_count}\n"
            f"Messages retained in the archive: {report.total_message_count}\n"
            f"New attachments saved: {report.decrypted_attachments}\n"
            f"Missing attachments: {report.missing_attachments}\n"
            f"Attachment errors: {report.attachment_errors}\n\n"
            f"Open:\n{report.index_file}"
        )
        messagebox.showinfo("Session archive complete", detail, parent=self.root)
        webbrowser.open(report.index_file.as_uri())

    def export_selected(self) -> None:
        selection = self.listbox.curselection()
        if not selection or not self.visible_conversations:
            messagebox.showinfo("Select a chat", "Select a conversation first.", parent=self.root)
            return
        conversation = self.visible_conversations[int(selection[0])]
        self._set_busy(True)
        self._append_log("--- Starting the permanent archive merge ---")
        try:
            require_session_stopped()
            self._run_export(conversation, self._output_parent())
        except ExportError as exc:
            self._handle_error("Export failed", exc)
            return
        except Exception as exc:
            self._handle_error("Unexpected export error", exc, unexpected=True)
            return
        finally:
            self._clean_work_area()
        self.conversations = []
        self.visible_conversations = []
        self.listbox.delete(0, END)
        self._set_busy(False)

    def quick_update(self) -> None:
        conversation = self._saved_conversation()
        if conversation is None:
            return
        self._set_busy(True)
        self._append_log("--- Starting a quick update of the remembered chat ---")
        try:
            output_parent = self._output_parent(saved=True)
            self.destination.set(str(output_parent))
            self._prepare_snapshot()
            self._run_export(conversation, output_parent)
        except ExportError as exc:
            self._handle_error("Quick update failed", exc)
            return
        except Exception as exc:
            self._handle_error("Unexpected quick-update error", exc, unexpected=True)
            return
        finally:
            self._clean_work_area()
        self._set_busy(False)

    def _handle_error(self, title: str, exc: Exception, unexpected: bool = False) -> None:
        self._append_log(f"ERROR: {title}: {exc}")
        cause = exc.__cause__
        while cause is not None:
            self._append_log(f"Caused by {type(cause).__name__}: {cause}")
            cause = cause.__cause__
        if unexpected:
            self._append_log("Technical traceback (safe to copy for troubleshooting):")
            for line in traceback.format_exc().rstrip().splitlines():
                self._append_log(line)
        self._clean_work_area()
        self._set_busy(False)
        messagebox.showerror(
            title,
            f"{exc}\n\nUse “Copy log to clipboard” and send the log for troubleshooting.",
            parent=self.root,
        )

    def close(self) -> None:
        self._clean_work_area()
        self.root.destroy()


def main() -> int:
    if os.name != "nt" and not os.environ.get("SESSION_EXPORT_PROFILE"):
        print("This application is intended for Windows.", file=sys.stderr)
        return 1
    root = Tk()
    SessionExporterApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
