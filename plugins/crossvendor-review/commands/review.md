---
description: Review the current change with lenses spread across the AI vendors you selected
argument-hint: '[--base <ref>] [--paths <path> ...] [--lens <ids>] [--dry-run]'
allowed-tools: Bash(python3:*), Bash(git:*), Read, Grep, Glob
---

Run a cross-vendor review of the current change and present a checked result.

Raw arguments: `$ARGUMENTS`

Rules:
- Review only. Do not edit files, apply fixes or commit.
- Every finding from a vendor is a claim to check, not a fact.

Steps:

1. Start the review. It makes one call per lens (nine by default) in parallel and can take several minutes, so run
   it with the Bash tool in the background (`run_in_background: true`) and wait for the completion notice:
   ```bash
   python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/cvr.py" review $ARGUMENTS
   ```
   If it fails with "no configuration", tell the user to run `/crossvendor-review:setup` first and stop.
   Exit code 3 means some lenses did not answer; continue, but say so.
2. Read the `report.md` and `findings.json` paths printed by the command.
3. For every finding (findings in the same group are usually one problem):
   - open the file at the reported line and read enough context to judge it;
   - keep it only if the code shows the problem; otherwise drop it and note why in one short phrase;
   - merge findings that describe the same problem and list every vendor that reported it.
4. Present:
   - a table of kept problems, most severe first: severity, `file:line`, the problem in one sentence, reported by;
   - a short list of dropped findings with the reason;
   - every lens that did not answer, by name, with its status. Never describe a run with missing lenses as clean.
5. If nothing survives the check and every lens answered, say that no reviewer found a problem that the code
   confirms. That is not a proof that the change is correct.

If the vendors cannot be reached because a sandbox blocks network access, report the failed lenses and ask the user
how they want to run it; do not change sandbox settings yourself.
