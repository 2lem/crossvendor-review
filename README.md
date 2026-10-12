# crossvendor-review

Code review and technical advice from several AI vendors at once, run from Claude Code.

- **Review**: nine review lenses (security, architecture, correctness, performance, tests, maintainability, data
  privacy, dependencies, adversarial) are spread across the vendors you select. Each lens sees the same change.
  Claude, as the host of your session, then checks every finding against the code and shows you what holds up.
- **Advise**: one technical question goes to every advisor model of your selected vendors. Claude compares the
  answers and tells you where they agree, where they differ and what it recommends.
- **Setup**: you pick the vendors you have; the tool assigns lenses and advisors and checks that each vendor is
  ready.

It does not claim that several vendors find more problems than one. It gives you independent views from different
model families and shows which vendor said what.

## Supported vendors

| Vendor | How it is called | Review model | Advisor models |
|---|---|---|---|
| Claude | `claude` CLI | `claude-opus-5-5` | `claude-opus-5-5`, `claude-fable-5-1` |
| Codex | `codex` CLI | `gpt-6.1-sol` (effort high) | `gpt-6-astra` |
| GLM | HTTPS, Z.ai API key | `glm-5.3` | `glm-5.3` |
| Grok | `grok` CLI | `grok-4.7` (effort xhigh) | `grok-4.7` |
| Muse | `muse` CLI | `muse-spark-1.3` | `muse-spark-1.3` |

Model ids and CLI flags were last verified on 2026-10-12. You can override models in the configuration file.

Claude is the host: inside Claude Code it does not run review lenses itself, so that the lenses come from other
vendors. Two exceptions, both shown in the lens table: if you select only Claude, the lenses run on Claude and the
result is labelled single-vendor and advisory; and if you select Claude plus one other vendor, a lens whose call
fails is retried on Claude, because there is no other vendor left to retry on.

## Requirements

- Claude Code, git and Python 3.9 or newer (no Python packages to install; tested on 3.9, 3.11 and 3.14).
- For each vendor you select: its CLI installed and logged in (Codex, Grok, Muse), or for GLM a pay-as-you-go
  API key in the `ZAI_API_KEY` environment variable.
- Every call uses your own subscription or API quota.

## Install

In Claude Code:

```
/plugin marketplace add 2lem/crossvendor-review
/plugin install crossvendor-review@crossvendor-review
/crossvendor-review:setup
```

Setup asks which vendors you have, writes the configuration, shows the lens table and checks each vendor.

## Use

```
/crossvendor-review:review
/crossvendor-review:review --base main
/crossvendor-review:review --paths src/billing
/crossvendor-review:advise Should the retry queue live in the API process or in a worker? --context src/queue.py
```

A review takes a few minutes: nine calls run in parallel. Results stay in your repository's git directory
(`.git/crossvendor-review/`), which is never committed. Each run keeps a copy of what was sent and what came back
there until you delete that directory.

### Without Claude Code

The engine is a plain Python script and works from any terminal:

```
python3 plugins/crossvendor-review/scripts/cvr.py setup --vendors claude,codex,grok
python3 plugins/crossvendor-review/scripts/cvr.py doctor --live
python3 plugins/crossvendor-review/scripts/cvr.py plan
python3 plugins/crossvendor-review/scripts/cvr.py review --base main
python3 plugins/crossvendor-review/scripts/cvr.py advise --question "..." --context src/app.py
```

Without Claude Code you get the raw findings and answers (`report.md`, `answers.md`); the checking step is done by
Claude inside Claude Code.

## What is sent where

| Command | What is sent | Sent to |
|---|---|---|
| review | The change: the diff and the new version of each changed file | The vendor of each lens |
| advise | Your question and the files you name (and the change, with `--diff`) | Every advisor model |
| setup, plan, doctor | Nothing (`doctor --live` sends one short test prompt per vendor) | - |

Before anything is sent:

- files with sensitive-looking paths (`.env`, private keys, credential files, `secrets/` directories) are left out;
- values that look like API keys, tokens, passwords (the password in a connection URL and Bearer tokens included)
  or private keys are replaced with `<redacted:...>`, in your question too;
- binary files, files over 200 KB, symbolic links and submodules are named but not included; for a file over 200 KB
  only its diff is sent, and only while that diff stays under 200 KB.

Redaction is pattern-based: it catches common key formats and `password = ...` style assignments, not every
secret. Do not rely on it for files that hold secrets in other forms.

The vendors are called without tools, web access or repository access: they see only that text. Check your
organization's rules before sending code to any vendor. Some plans may use submitted content for training; the data
privacy lens is placed on another vendor than GLM when you select one.

## How lenses are assigned

The assignment is deterministic: the same vendor selection always gives the same table.

1. The host vendor does not review its own session; lenses go to the other selected vendors.
2. Lenses are placed in a fixed order on the least-loaded vendor. You can pin a lens to a vendor with
   `setup --pin security=codex`. Running setup again without `--pin` keeps the earlier pins that are still valid;
   running it with `--pin` replaces them. A pin that overrides a placement rule (for example the data privacy lens
   on GLM) is shown as a warning.
3. If a lens call fails (quota, timeout, login, or an answer without usable findings), it is retried once on its
   fallback vendor, shown in the lens table. The fallback follows the same placement rules as the lens (the data
   privacy lens does not fall back to GLM), and is the host only when no other vendor is left. A lens that still
   has no answer is reported as missing, never as clean.
4. Every advisor model of every selected vendor answers questions. The diversity score counts 1.0 per vendor (0.5 if
   its only model is the host's own model); 2.0 or more means at least two independent vendors answered.

Run `/crossvendor-review:setup` again at any time to change the selection.

## Configuration

The file is `~/.config/crossvendor-review/config.json` (or `$XDG_CONFIG_HOME/...`, or the path in `$CVR_CONFIG`).
It never holds credentials.

```json
{
  "schema_version": 1,
  "vendors": ["claude", "codex", "grok"],
  "host": "claude",
  "host_model": "claude-opus-5-5",
  "pins": {"security": "codex"},
  "overrides": {"codex": {"review_model": "gpt-6.1-sol", "review_effort": "high"}},
  "guideline_files": ["CONTRIBUTING.md"],
  "timeout_s": 1200,
  "max_parallel": 9,
  "max_packet_chars": 400000
}
```

- `overrides` accepts `review_model`, `review_effort`, `advisor_models` and `advisor_effort` per vendor.
- `guideline_files` are sent to the maintainability lens so it can check the change against your project rules.
  They are not sent unless you list them.

## Limitations

- Reviewers see only the change and the changed files, not the rest of the repository.
- Large changes must be narrowed with `--paths` (the default limit is 400,000 characters per call).
- Vendor CLIs change their flags; if a vendor fails after an update, `doctor --live` shows the error.
- On macOS the Codex call also hides your global Codex instruction files; on Linux it does not.

## Tests

```
python3 -m unittest discover -s tests
```

The tests use stand-in vendor programs and a local HTTP server; they make no network calls.

## License

MIT, see [LICENSE](LICENSE).
