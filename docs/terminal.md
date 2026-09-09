# Chat with Theo in your terminal

Start the assistant normally, then open a second terminal:

```sh
# Terminal 1, unless your Theo service is already running:
uv run --no-sync theo serve

# Terminal 2:
uv run --no-sync theo chat
```

Restart the daemon after updating an existing installation so the client and service use the same implementation. Use the same `--data-root` in both terminals when you have a custom installation:

```sh
uv run --no-sync theo --data-root /absolute/path/to/theo chat
```

This is a local client of the running assistant. It uses Theo's existing subscription adapters, tool grants, durable jobs and SQLite memory. It starts no second model process or Telegram poller of its own. Telegram and terminal conversations are separate, while owner memory is shared. Native account verification and OS isolation requirements still apply.

## Write, paste and attach

Enter sends a message. Alt+Enter inserts a newline; on terminals that intercept that shortcut, press Escape then Enter. Multiline paste stays in the input buffer for review before sending. Up/Down recall this terminal's input history. Tab completes slash commands and paths after `/attach`.

```text
You › Explain this function and suggest a simpler implementation.

You › /attach ~/Documents/design.pdf
Attached: design.pdf
You [1 attached] › What are the main trade-offs?

You › Compare @./before.png @"./screenshots/after update.png"
```

You can also drag files into the terminal, or paste a complete file path and press Enter. Theo stages existing paths as attachments instead of sending them as a question. Quoted paths, shell-escaped spaces and local `file://` URLs are supported. Nothing executes as a shell command.

Use `/attachments` to inspect staged files and `/clear-attachments` to remove them. Sending an empty message with files attached asks Theo to inspect them. Files are copied into Theo's artifact store at send time, so later edits or deletion of the originals do not change the submitted input.

Attach up to eight files with a combined limit of 20 MiB by default (`max_media_bytes` in configuration). Images in PNG, JPEG, WebP and GIF formats use the native adapter's existing vision path; the selected model must support images. Text, code, Markdown, PDF and supported documents use local extraction. Files with no supported extraction are preserved with that limitation stated to the model. Directories are not recursively uploaded. Clipboard image **bytes** are not supported: save the image and paste/drag its file path.

## Read responses

Theo displays Markdown, headings, lists, tables and highlighted fenced code blocks. Visible answer text appears as a **live draft** when the backend emits it; some backends emit larger chunks. Tool names and job state appear above the response. Only visible answer text is streamed, not hidden reasoning. Previews are capped at 100,000 characters per run.

The final response comes from the canonical job/action records. The terminal waits for local final delivery to be recorded, so the next turn sees the previous response in history. Auth/quota waits, failures, interruptions and delivery states are shown explicitly. A draft is never presented as a completed action.

## Conversations and controls

```sh
uv run --no-sync theo chat --session work
uv run --no-sync theo chat --session personal
uv run --no-sync theo chat --backend codex --model YOUR_INCLUDED_MODEL
uv run --no-sync theo chat --attach ./screenshot.png
```

The default named conversation resumes automatically. Recent messages appear when you reopen it. Model choices persist per conversation; they do not alter Telegram's route.

| Command | Action |
| --- | --- |
| `/help` | Show controls and examples |
| `/model` | Show this conversation's backend/model |
| `/model BACKEND MODEL` | Select an included model on codex, claude, cursor or grok |
| `/new` | Start a fresh named conversation, retaining shared memory |
| `/resume NAME` | Switch to an existing or new named conversation |
| `/history` | Display the latest 20 user/assistant messages |
| `/wait` | Follow the most recent job again |
| `/cancel` | Cancel the most recent job and its dependent work |
| `/status` | Inspect daemon/job status |
| `/quit` or Ctrl+D at the prompt | Disconnect without stopping Theo |

Ctrl+C during a response requests cancellation. The daemon observes it in its normal loop and stops the active worker; already completed external effects remain recorded. Ctrl+C at the prompt clears the current input. If the daemon stops, the terminal stops waiting and tells you how to reconnect; the durable job remains inspectable. An unfinished job must be followed or cancelled before sending another message in the same conversation.

## Existing scripted CLI

Passing text still uses the existing JSON-returning queue command. Attachments work here too:

```sh
uv run --no-sync theo chat "Summarize this document" --attach ./design.pdf
```

This preserves existing scripts. Omit the text argument for the interactive interface; it requires a real terminal. Shell input history is in memory for the current client only, while submitted conversation history is stored in Theo's canonical SQLite records.

## Testing

`tests/test_terminal.py` covers path parsing, copied attachments, image preparation, coordinator draft/final delivery, reconnection, daemon loss, cancellation, formatted output and the actual prompt's exit behavior using synthetic data and an offline backend fixture. It does not claim live subscription qualification. These tests run in the free Linux/macOS CI workflow.
