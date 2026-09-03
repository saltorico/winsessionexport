# Windows Session Chat Exporter

This folder is a clean Windows port of the Session HTML exporter. It is designed
to export one conversation on the same Windows computer where Session Desktop is
installed.

It automatically locates the Session profile at:

```text
%APPDATA%\Session
```

It reads the SQLCipher database key from `%APPDATA%\Session\config.json` in
memory. The key is never displayed, printed, placed in a command-line argument,
written to the export, or sent over the network. The attachment key is read from
the temporary decrypted database in memory. Python cannot guarantee that an
immutable string is securely zeroed, but all references are dropped promptly and
the temporary working directory is removed when the program closes.

## User workflow

Keep `SessionChatExporter.exe` in Downloads. On the first run:

1. Fully quit Session from the Windows system-tray icon.
2. Open `SessionChatExporter.exe`.
3. Click **1. Find my Session chats**. The activity log explains each database-copy,
   decryption, and validation step.
4. Search the conversation list by name, ID, or recent-message preview and select the
   intended chat.
5. Click **3. Export / update selected chat**.
6. The app creates `Downloads\session-export\index.html`, saves decrypted attachments
   under `Downloads\session-export\attachments`, and opens the HTML in the browser.

On later runs, fully quit Session and click **Next time: update my saved chat now**.
The app takes a fresh snapshot and merges only material it has not archived before.
It does not create timestamped folders. Message IDs provide deduplication, so the merge
also works when a 12-hour disappearing-message window has no overlap with the previous
capture. Earlier captures remain in the accumulating HTML conversation.

If anything fails, click **Copy log to clipboard** and send the pasted log for
troubleshooting. The database key is never included in the log.

The exporter never writes to Session's live profile. It copies only the encrypted
database into a private Windows temporary directory, decrypts a temporary copy,
reads selected attachment blobs from `attachments.noindex`, and removes the
temporary databases after the operation. The HTML, decrypted attachments, and the
incremental `archive.json` index remain in `session-export` and must be kept private.

## Run from source on Windows

Install 64-bit Python, then double-click:

```text
Setup Windows.cmd
```

After setup completes, double-click:

```text
Export Session Chat.cmd
```

Python is the only manual prerequisite. Setup installs pinned Windows wheels for
`sqlcipher3` and `PyNaCl`; no WSL, Git, DB Browser, standalone SQLite, standalone
SQLCipher, or standalone libsodium installation is required.

## Build the self-contained executable

PyInstaller must build the executable on Windows. From PowerShell on Windows:

```powershell
./build_windows.ps1
```

The result is:

```text
dist\SessionChatExporter.exe
```

From a Mac, make the contents of this `winsessionexport` folder the root of a
clean private GitHub repository. The included workflow at
`.github/workflows/build-windows.yml` builds on a Windows x64 runner and provides
the executable as a downloadable Actions artifact.

Never add a real `config.json`, database, attachments, snapshot, or export to the
build repository. The included `.gitignore` blocks the expected sensitive names,
but inspect staged files before every commit.

## Security and limitations

- Session must remain fully closed while conversations are loaded and exported.
- The temporary plaintext database is deleted normally, not securely overwritten;
  modern SSDs and Windows storage make reliable per-file secure erasure impractical.
- The finished export is plaintext and can be read by anyone with access to it.
- The generated executable is unsigned unless a separate code-signing step is
  configured, so Windows SmartScreen may warn on first launch.
- Test the executable with synthetic data before using a real Session profile.

## Tests

The tests create only synthetic SQLite databases and attachments:

```powershell
python -m unittest discover -s tests -v
```
