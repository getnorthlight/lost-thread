# Add your memory tool

Lost Thread works for any memory tool that exposes an MCP server.

1. **Ingest the week.** After `bench.build`, `personas/<p>/events.jsonl` holds the week as one JSON row per event,
   oldest first: `id`, `at` (UTC), `kind` (`app.activated`, `window.changed`, `mouse.click`, `keyboard.text_input`,
   `keyboard.submit`), `app`, `bundle`, `title`, `url` (Chrome: origin only), `page` (the full link, Chrome only),
   `text` (typed words, `keyboard.text_input` only) and capture details. Load it into your tool however it stores
   history. Don't read `ledger.json` or `questions.jsonl`: those are the answers.
2. **Plug it in as the memory server.**
   ```bash
   export LT_MCP_NAME=mytool
   export LT_MCP_COMMAND='{"command": "/path/to/mytool", "args": ["mcp", "--data", "/path/to/week"], "env": {}}'
   python3 -m bench.harness --persona developer --run-id mytool-developer --arms native,native+dd,dd --new-day
   ```
   In the `+dd` arms the contestant gets your server's tools (and its MCP `instructions`) and nothing else; the
   system prompt and question are identical to the DayDream runs. Each answer and tool call lands in
   `runs/<run-id>/traces.jsonl`, and the pack, judges and scorer work unchanged.
3. **Report it** with the version of your tool and the day you ran it.
