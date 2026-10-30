# ultra-RAG

Local-first memory and key vault for [DeepSeek Harness](https://github.com/deepseek-ai) (`dsh`).

ultra-RAG keeps reusable knowledge, session excerpts, encrypted API keys, and password-input handoff on your own machine. It is designed to run on a **single PC**; a home server and multi-device sync are optional, not required.

> Status: experimental. The CLI and storage format may change before a stable release.

## What it does

- **Knowledge memory** 窶・stores generalizable principles, lessons, and decisions; not copies of README or design documents.
- **Scoped recall** 窶・universal memories, project-only memories, and session notes are kept separate.
- **Encrypted key vault** 窶・AES-GCM values with Windows DPAPI or Linux `systemd-creds` protection; values are supplied only to a selected child process.
- **Password handoff** 窶・one-shot sudo/SSH password input without writing the password to disk.
- **MCP server** 窶・exposes memory and key tools to dsh over stdio.
- **Optional server sync** 窶・a home server can be authoritative, while a PC keeps a replica and an unsynced queue.

The public build does **not** include a hosted service, telemetry, or a shared cloud database. Your data stays in your local application-data directory unless you explicitly configure sync.

## Single-PC mode

This is the default and recommended mode.

- Run `ultra-rag init` on one PC.
- Leave `sync_remote` unset.
- Do not start `rag serve`, systemd services, or a reverse proxy.
- Use `ULTRA_RAG_HOME` if you want the runtime data in a specific local directory.

The tool works without a server, account, or network connection. Server sync is an optional extension for a second device.

## Requirements

- Python 3.11 or newer
- Windows 10/11 or Linux
- Optional: `dsh` for automatic memory injection

## Install

```bash
git clone https://github.com/puyokura/ultra-rag.git
cd ultra-rag
python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\activate
python -m pip install -U pip
pip install -e .
ultra-rag init
```

Linux/macOS:

```bash
. .venv/bin/activate
python -m pip install -U pip
pip install -e .
ultra-rag init
```

`init` creates the runtime home in the platform application-data directory. Set `ULTRA_RAG_HOME` to choose another location.

## Basic use

```bash
# Save a reusable principle
ultra-rag rag remember "Verify with a real process, not only with unit tests" \
  --kind lesson --scope shared_lesson

# Search
ultra-rag rag search "real process"

# Show the injected profile, constraints, and related memories
ultra-rag rag snapshot --query "testing" --text

# Register a key locally (input is hidden; the value is not passed in argv)
ultra-rag secret add EXAMPLE_API_KEY --service example

# List metadata only; values are never returned
ultra-rag secret list

# Give a selected key to one child process only
ultra-rag secret exec \
  --key <KEY_ID> \
  --command 'python -c "import os; print(bool(os.environ.get(\"EXAMPLE_API_KEY\")))"'
```

The CLI prints JSON by default. There is no `--json` option.

## Key files

| Purpose | File |
| --- | --- |
| Secret values | `.env` (ignored by Git; import with `secret import-env`) |
| Non-secret settings | `.env.public` (ignored by Git; source it in your shell) |
| Templates | `.env.example`, `.env.public.example` |

Never commit `.env` or real keys. The importer and vault are owner-side tools; they do not send values to a model.

## dsh integration (optional)

The repository includes a dsh Cordis plugin and an MCP stdio server.

```bash
ultra-rag mcp
ultra-rag overlay --output dsh-overlay.yml
```

Register the overlay in your dsh profile, or use the generated overlay as a reference. The plugin injects memory before a turn and records important excerpts after a turn. The MCP server exposes `memory_*`, `key_*`, and execution-status tools.

If you do not use dsh, the CLI works on its own.

## Home server / sync (optional)

A single PC works without any server. If you want a second device:

```bash
# On the authoritative host
ultra-rag rag serve --host 127.0.0.1 --port 8765

# On the client
ultra-rag rag sync once
```

Use a private network or a TLS reverse proxy. Do not expose the HTTP API directly to the internet. See the service examples under `secret-core/systemd/`.

## Security notes

- The key vault protects values at rest; a command that receives a key can read it.
- Redaction reduces accidental output but cannot prevent a program from transforming or transmitting a value.
- Password handoff is one-shot and does not store the password.
- Use a dedicated non-administrator account for a server deployment.
- Back up the runtime home before deleting keys or recreating the vault.

## Development

```bash
python -m unittest discover -s tests -t .
```

The suite includes Windows- and Linux-specific skips. See `tests/` for the actual coverage.

## License

No license has been selected yet. Do not assume permission to redistribute until a license is added.
