---
description: Ask every advisor model of your selected vendors the same technical question and compare the answers
argument-hint: '<question> [--context <file> ...] [--diff]'
allowed-tools: Bash(python3:*), Bash(git:*), Read, Write
---

Get independent advice from several AI vendors on one question.

Raw arguments: `$ARGUMENTS`

Steps:

1. Write the question (the argument text without any `--context`, `--diff` or `--base` options) to a temporary
   file, so quoting cannot break it. If the question depends on files, pass them with `--context`; pass `--diff`
   when it is about the current change. Only the question and these files are sent.
2. Run it with the Bash tool in the background (`run_in_background: true`) and wait for the completion notice:
   ```bash
   python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/cvr.py" advise --question-file <tmp-file> [--context <files>] [--diff]
   ```
   If it fails with "no configuration", tell the user to run `/crossvendor-review:setup` first and stop.
3. Read the `answers.md` path it prints.
4. Present:
   - where the advisors agree and where they differ, naming the vendors;
   - any claim an advisor makes about this repository: check it against the code before repeating it, and mark the
     ones you could not check;
   - your own recommendation, with the reason and the strongest counter-argument from the answers.
   - the diversity line from the output; if it says "advisory (low diversity)", say that fewer than two independent
     vendors answered.

Agreement between vendors is not evidence by itself. Do not edit project files.
