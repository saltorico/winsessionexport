"""Small Tkinter front end for the Windows Session Chat Exporter."""

from __future__ import annotations

import os
import sys
import tempfile
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, Y, Button, Frame, Label, Listbox, Scrollbar, StringVar, Tk
from tkinter import filedialog, messagebox

from session_export_core import (
    Conversation,
    ExportError,
    default_documents_directory,
    export_conversation,
    list_conversations,
    prepare_plaintext_database,
    require_session_stopped,
    session_profile_from_environment,
    validate_profile,
)


class SessionExporterApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("Session Chat Exporter")
        self.root.geometry("720x500")
        self.root.minsize(580, 380)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.status = StringVar(value="Quit Session completely, then load your conversations.")
        self.conversations: list[Conversation] = []
        self.work_area: tempfile.TemporaryDirectory[str] | None = None
        self.plaintext_database: Path | None = None
        self.profile = None

        Label(
            root,
            text="Export one Session conversation",
            font=("Segoe UI", 16, "bold"),
            anchor="w",
        ).pack(fill="x", padx=18, pady=(18, 4))
        Label(
            root,
            text=(
                "Everything stays on this computer. The database key is read automatically "
                "and is never displayed or included in the export."
            ),
            justify="left",
            anchor="w",
            wraplength=675,
        ).pack(fill="x", padx=18, pady=(0, 12))

        controls = Frame(root)
        controls.pack(fill="x", padx=18)
        self.load_button = Button(
            controls,
            text="1. Load conversations",
            command=self.load_conversations,
            padx=12,
            pady=6,
        )
        self.load_button.pack(side=LEFT)
        self.export_button = Button(
            controls,
            text="2. Export selected conversation",
            command=self.export_selected,
            padx=12,
            pady=6,
            state="disabled",
        )
        self.export_button.pack(side=LEFT, padx=(10, 0))

        list_frame = Frame(root)
        list_frame.pack(fill=BOTH, expand=True, padx=18, pady=14)
        scrollbar = Scrollbar(list_frame, orient=VERTICAL)
        scrollbar.pack(side=RIGHT, fill=Y)
        self.listbox = Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            font=("Segoe UI", 10),
            activestyle="dotbox",
            exportselection=False,
        )
        self.listbox.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.config(command=self.listbox.yview)
        self.listbox.bind("<Double-Button-1>", lambda _event: self.export_selected())

        Label(root, textvariable=self.status, anchor="w", relief="sunken").pack(
            fill="x", side="bottom", padx=0, pady=0
        )

    def _set_busy(self, busy: bool, status: str) -> None:
        self.status.set(status)
        self.load_button.configure(state="disabled" if busy else "normal")
        export_state = "disabled" if busy or not self.conversations else "normal"
        self.export_button.configure(state=export_state)
        self.root.update_idletasks()

    def _clean_work_area(self) -> None:
        self.plaintext_database = None
        if self.work_area is not None:
            self.work_area.cleanup()
            self.work_area = None

    def load_conversations(self) -> None:
        self._clean_work_area()
        self.conversations = []
        self.listbox.delete(0, END)
        self._set_busy(True, "Checking Session and preparing a private temporary database...")
        try:
            require_session_stopped()
            self.profile = session_profile_from_environment()
            validate_profile(self.profile)
            self.work_area = tempfile.TemporaryDirectory(prefix="SessionChatExporter-")
            self.plaintext_database = prepare_plaintext_database(
                self.profile, Path(self.work_area.name)
            )
            self.conversations = list_conversations(self.plaintext_database)
            for conversation in self.conversations:
                self.listbox.insert(END, conversation.display_label)
            self.listbox.selection_set(0)
            self.listbox.activate(0)
            self._set_busy(
                False,
                f"Loaded {len(self.conversations)} conversations. Select one to export.",
            )
        except ExportError as exc:
            self._clean_work_area()
            self._set_busy(False, "Could not load conversations.")
            messagebox.showerror("Session Chat Exporter", str(exc), parent=self.root)
        except Exception:
            self._clean_work_area()
            self._set_busy(False, "Could not load conversations.")
            messagebox.showerror(
                "Session Chat Exporter",
                "An unexpected error occurred while preparing the Session database.",
                parent=self.root,
            )

    def export_selected(self) -> None:
        if not self.conversations or self.plaintext_database is None or self.profile is None:
            return
        selection = self.listbox.curselection()
        if not selection:
            messagebox.showinfo(
                "Session Chat Exporter", "Select a conversation first.", parent=self.root
            )
            return
        conversation = self.conversations[int(selection[0])]
        destination = filedialog.askdirectory(
            parent=self.root,
            title="Choose where to save the exported chat",
            initialdir=str(default_documents_directory()),
            mustexist=True,
        )
        if not destination:
            return

        self._set_busy(True, f"Exporting {conversation.label}...")
        try:
            require_session_stopped()
            report = export_conversation(
                self.plaintext_database,
                self.profile.attachments,
                conversation,
                Path(destination),
            )
            self._set_busy(False, f"Export complete: {report.output_directory}")
            messagebox.showinfo(
                "Export complete",
                (
                    f"Exported {report.message_count} messages and "
                    f"{report.decrypted_attachments} attachments.\n\n"
                    f"Saved to:\n{report.output_directory}"
                ),
                parent=self.root,
            )
            webbrowser.open(report.index_file.as_uri())
        except ExportError as exc:
            self._set_busy(False, "Export failed.")
            messagebox.showerror("Session Chat Exporter", str(exc), parent=self.root)
        except Exception:
            self._set_busy(False, "Export failed.")
            messagebox.showerror(
                "Session Chat Exporter",
                "An unexpected error occurred while exporting the conversation.",
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

