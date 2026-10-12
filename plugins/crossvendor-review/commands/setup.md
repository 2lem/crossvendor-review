---
description: Choose the AI vendors you use and preview how review lenses and advisors are assigned
argument-hint: '[comma-separated vendors, for example codex,grok]'
allowed-tools: Bash(python3:*), AskUserQuestion
---

Set up crossvendor-review for this user.

Raw arguments: `$ARGUMENTS`

1. Decide the vendor list.
   - Claude is always included: it is the host that runs this session and weighs the results.
   - If the arguments name vendors (any of `codex`, `glm`, `grok`, `muse`), use them.
   - Otherwise ask once with `AskUserQuestion`, `multiSelect: true`, question "Which other AI vendors do you have
     access to?", options:
     - `Codex` - OpenAI Codex CLI, logged in with a ChatGPT plan or API key
     - `GLM` - Z.ai API key in the `ZAI_API_KEY` environment variable
     - `Grok` - xAI Grok CLI, logged in
     - `Muse` - Meta Muse CLI, logged in
2. Run, with the lower-case ids joined by commas and `claude` first:
   ```bash
   python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/cvr.py" setup --vendors claude,<chosen>
   ```
3. Run the readiness check and show its table verbatim:
   ```bash
   python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/cvr.py" doctor
   ```
4. Explain in two or three sentences:
   - which vendor runs which lens (from the setup output) and the advisor diversity line;
   - for every row that is not `READY`, the exact fix from the table. `INSTALLED` means the CLI is present but its
     login cannot be checked offline; offer `doctor --live`, which makes one short test call per vendor.
   - If only Claude was selected, say that results will be single-vendor and advisory.

Do not print or ask for API keys. Do not edit the configuration file by hand.
