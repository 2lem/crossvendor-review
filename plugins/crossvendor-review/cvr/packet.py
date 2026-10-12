"""Build the text that is sent to the vendors: the change under review, or a question with context files.

Exactly this text is sent, nothing else. Before it is built:
  - files whose path looks sensitive (.env files, private keys, credential files, secrets/ directories) are left out
    of both the diff and the file list, and only counted, never named (a name can say as much as the file); a
    context or guideline file is checked by its resolved target too, and a relative path that resolves outside the
    folder (through a link, for example) is refused and counted the same way;
  - values that look like API keys, tokens, passwords or private keys are replaced with <redacted:...>, in the
    question, file names and the base name as well; line breaks inside a replaced value are kept so line numbers
    stay right;
  - binary files, files over 200 KB, symbolic links and directories (submodules) are named but not included; the
    diff of a file over 200 KB is kept only while the diff itself stays under 200 KB.
"""

import os
import re
import subprocess

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_FILE_BYTES = 200_000
TOO_LARGE = "larger than %d KB" % (MAX_FILE_BYTES // 1000)
SENSITIVE_PATH_RE = re.compile(
    r"(^|/)(\.env(\.[^/]*)?|\.netrc|\.npmrc|\.pypirc|\.pgpass|\.git-credentials|id_rsa|id_dsa|id_ecdsa|id_ed25519"
    r"|credentials(\.json)?|service[-_]?account[^/]*\.json)$|\.(pem|key|p12|pfx|keystore|jks)$"
    r"|(^|/)\.?secrets?/|(^|/)\.aws/|(^|/)\.docker/config\.json$|(^|/)\.kube/config$", re.I)
NAME_SUFFIX = r"(?:[_-]?(?:access[_-]?)?(?:key|value))?"  # secret_key, token_value, secret_access_key
PASSWORD_NAME = r"(?:password|passwd|pwd|passphrase)" + NAME_SUFFIX
SECRET_NAME = r"(?:api[_-]?key|secret|token|password|passwd|pwd|passphrase|private[_-]?key|credentials?)" + NAME_SUFFIX
MIN_UNQUOTED = {"assigned-password": 4, "assigned-secret": 16}
DIFF_ARGS = ["diff", "--no-color", "--no-ext-diff", "--no-renames", "-U3"]
SECRET_RES = [
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}")),
    ("xai-key", re.compile(r"xai-[A-Za-z0-9]{20,}")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("slack-token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
                               re.S)),
    # The password inside a connection URL: scheme://user:PASSWORD@host
    ("url-password", re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:)([^\s@/'\"]+)(@)")),
    ("bearer-token", re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/=-]{16,})")),
    # A quoted value after a secret-like name is replaced whole, whatever its length or characters; the name may be
    # quoted too, as in JSON: {"password": "correct horse"}.
    ("quoted-secret", re.compile(r"(?i)(" + SECRET_NAME + r"['\"]?\s*[:=]\s*)"
                                 r"(?:(\")((?:[^\"\\\n]|\\.)+)\"|(')((?:[^'\\\n]|\\.)+)')")),
    # Unquoted values: after a password-like name from 4 characters, after other secret names from 16 (MIN_UNQUOTED).
    # The whole run of value characters is taken at once and left alone when "(" follows (a function call), so the
    # pattern never backtracks over a long line.
    ("assigned-password", re.compile(r"(?i)(" + PASSWORD_NAME + r"['\"]?\s*[:=]\s*)([^\s'\",;()<>]+)(\(?)")),
    ("assigned-secret", re.compile(r"(?i)(" + SECRET_NAME + r"['\"]?\s*[:=]\s*)([^\s'\",;()<>]+)(\(?)")),
]


class PacketError(Exception):
    pass


class NothingToReview(PacketError):
    """The working tree has no change against the chosen base."""


def git(repo, args, check=True, stdin=None):
    # Paths are always literal: a file named "*" must not act as a pattern that pulls a left-out file back in.
    p = subprocess.run(["git", "--literal-pathspecs", "-c", "core.quotepath=off"] + args, cwd=repo,
                       capture_output=True, input=stdin)
    if check and p.returncode != 0:
        raise PacketError("git %s failed: %s" % (" ".join(args[:2]), p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout.decode("utf-8", "replace")


def repo_root(path="."):
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True)
    if p.returncode != 0:
        raise PacketError("not inside a git repository: %s" % os.path.abspath(path))
    return p.stdout.strip()


def redact(text):
    """Return (text, count) with secret-looking values replaced."""
    count = [0]
    for name, rx in SECRET_RES:
        def repl(m, name=name):
            if name in MIN_UNQUOTED:
                if m.group(3) or len(m.group(2)) < MIN_UNQUOTED[name]:
                    return m.group(0)
                count[0] += 1
                return m.group(1) + "<redacted:%s>" % name
            count[0] += 1
            if name == "quoted-secret":
                quote = m.group(2) or m.group(4)
                return m.group(1) + quote + "<redacted:%s>" % name + quote
            if name == "url-password":
                return m.group(1) + "<redacted:%s>" % name + m.group(3)
            if name == "bearer-token":
                return m.group(1) + "<redacted:%s>" % name
            # keep the line breaks of a multi-line value, so line numbers after it stay right
            return "<redacted:%s>" % name + "\n" * m.group(0).count("\n")
        text = rx.sub(repl, text)
    return text, count[0]


def sensitive(path):
    return SENSITIVE_PATH_RE.search(path) is not None


# Files left out for these reasons are counted in the text sent, not named.
COUNTED = {"sensitive path": "a sensitive-looking path", "resolves outside the folder": "a path outside the folder"}


def not_included(omitted):
    """(line, redactions): the line that tells the vendor which files were left out."""
    named, counted, redactions = [], {}, 0
    for path, reason in omitted:
        if reason in COUNTED:
            counted[reason] = counted.get(reason, 0) + 1
            continue
        shown, n = redact(path)
        redactions += n
        named.append("%s (%s)" % (shown, reason))
    for reason in sorted(counted):
        named.append("%d file%s with %s" % (counted[reason], "" if counted[reason] == 1 else "s", COUNTED[reason]))
    return ("Not included: " + "; ".join(named) if named else ""), redactions


def numbered(text):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join("%5d| %s" % (i, line) for i, line in enumerate(lines, 1))


def read_text(path, follow_links=False):
    """(text, reason) where reason explains why the text is not included. A symbolic link is not followed unless
    asked: it could point outside the repository (a key file, for example)."""
    if os.path.islink(path) and not follow_links:
        return None, "symbolic link"
    if os.path.isdir(path):
        return None, "directory (for example a submodule)"
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None, TOO_LARGE
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None, "unreadable"
    if len(data) > MAX_FILE_BYTES:
        return None, TOO_LARGE
    if b"\x00" in data:
        return None, "binary"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "not UTF-8 text"


def _split_z(text):
    return [p for p in text.split("\x00") if p]


def review_target(repo, base=None, paths=None):
    """What to review: changes from HEAD (or from the merge base with `base`) to the working tree, untracked files
    included. Returns (rev, [(status, path)])."""
    has_head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD^{commit}"], cwd=repo,
                              capture_output=True).returncode == 0
    if base:
        if not has_head:
            raise PacketError("--base needs at least one commit")
        rev = git(repo, ["merge-base", base, "HEAD"]).strip()
    else:
        rev = "HEAD" if has_head else EMPTY_TREE
    spec = ["--"] + list(paths or [])
    rows = _split_z(git(repo, ["diff", "--name-status", "--no-renames", "-z", rev] + spec))
    changes = [(rows[i], rows[i + 1]) for i in range(0, len(rows) - 1, 2)]
    for path in _split_z(git(repo, ["ls-files", "--others", "--exclude-standard", "-z"] + spec)):
        changes.append(("?", path))
    return rev, sorted(changes, key=lambda c: c[1])


def review_packet(repo, base=None, paths=None, max_chars=400000, guideline_files=()):
    """Return (packet_text, guidelines_text, stats). Raises PacketError when there is nothing to review or the
    packet is too large."""
    rev, changes = review_target(repo, base, paths)
    if not changes:
        raise NothingToReview("nothing to review: no changes against %s" % ("the merge base" if base else "HEAD"))
    omitted, included, redactions = [], [], 0
    keep = []
    for status, path in changes:
        if sensitive(path):
            omitted.append((path, "sensitive path"))
        else:
            keep.append((status, path))
    tracked = [p for s, p in keep if s != "?"]
    # The size limit applies to the diff too: a large file must not travel in full as added or removed lines. A small
    # change to a large file keeps its diff (only the full listing of the file is left out).
    large, diff_only = set(), set()
    for path in large_in_diff(repo, rev, tracked):
        if "\n" in path or len(git(repo, DIFF_ARGS + [rev, "--", path]).encode("utf-8")) > MAX_FILE_BYTES:
            large.add(path)
            omitted.append((path, TOO_LARGE + "; left out of the diff too"))
        else:
            diff_only.add(path)
    keep = [(s, p) for s, p in keep if p not in large]
    tracked = [p for p in tracked if p not in large]
    diff = git(repo, DIFF_ARGS + [rev, "--"] + tracked) if tracked else ""
    diff, n = redact(diff)
    redactions += n
    shown_base, n = redact(base or "")
    redactions += n
    parts = ["=== CHANGE PACKET ===",
             "A change to an existing project. The diff is against %s; each changed file follows in its new version "
             "with line numbers." % ("the merge base with " + shown_base if base else "the last commit"), "",
             "--- DIFF ---", diff.rstrip("\n") or "(no tracked changes; new files only)", "--- END DIFF ---"]
    for status, path in keep:
        if status.startswith("D"):
            continue
        text, reason = read_text(os.path.join(repo, path))
        if text is None:
            omitted.append((path, reason + ("; only its diff is included" if path in diff_only else "")))
            continue
        text, n = redact(text)
        shown, k = redact(path)
        redactions += n + k
        label = "new, untracked" if status == "?" else ("new" if status.startswith("A") else "modified")
        parts += ["", "--- FILE %s (%s) ---" % (shown, label), numbered(text), "--- END FILE ---"]
        included.append(path)
    line, n = not_included(omitted)
    redactions += n
    if line:
        parts += ["", line]
    parts += ["=== END PACKET ==="]
    packet = "\n".join(parts) + "\n"
    guidelines, gn, skipped = guidelines_text(repo, guideline_files)
    redactions += gn
    omitted += skipped  # shown in the user's own report only
    if len(packet) + len(guidelines) > max_chars:
        biggest = sorted(included, key=lambda p: -os.path.getsize(os.path.join(repo, p)))[:5]
        raise PacketError("the change is too large to send (%d characters, limit %d); narrow it with --paths. "
                          "Largest files: %s" % (len(packet) + len(guidelines), max_chars, ", ".join(biggest)))
    stats = {"rev": rev, "files": included, "omitted": omitted, "redactions": redactions, "chars": len(packet),
             "guideline_chars": len(guidelines)}
    return packet, guidelines, stats


def large_in_diff(repo, rev, paths):
    """Paths whose old or new version is over the size limit (a name with a line break counts as large, since it
    cannot be checked line by line)."""
    large = {p for p in paths if "\n" in p}
    rest = [p for p in paths if p not in large]
    for p in rest:
        full = os.path.join(repo, p)
        if os.path.isfile(full) and not os.path.islink(full) and os.path.getsize(full) > MAX_FILE_BYTES:
            large.add(p)
    if rest and rev != EMPTY_TREE:
        out = git(repo, ["cat-file", "--batch-check"], stdin=("".join("%s:%s\n" % (rev, p) for p in rest))
                  .encode("utf-8"))
        for p, row in zip(rest, out.splitlines()):
            fields = row.split()
            if len(fields) == 3 and fields[1] == "blob" and fields[2].isdigit() and int(fields[2]) > MAX_FILE_BYTES:
                large.add(p)
    return large


def guidelines_text(repo, files):
    """(text, redactions, skipped). A guideline file must stay inside the repository once links are resolved (a
    linked directory on the way included), its resolved path must not look sensitive, and it must not be a link."""
    parts, redactions, skipped = [], 0, []
    root = os.path.realpath(repo)
    for rel in files:
        full = os.path.join(repo, rel)
        real = os.path.realpath(full)
        if not real.startswith(root + os.sep):
            skipped.append((rel, "guideline file outside the repository"))
            continue
        if sensitive(rel) or sensitive(os.path.relpath(real, root)):
            skipped.append((rel, "guideline file with a sensitive path"))
            continue
        if not os.path.isfile(full):
            skipped.append((rel, "guideline file not found"))
            continue
        text, reason = read_text(full)
        if text is None:
            skipped.append((rel, "guideline file: " + reason))
            continue
        text, n = redact(text)
        shown, k = redact(rel)
        redactions += n + k
        parts += ["--- GUIDELINES %s ---" % shown, text.rstrip("\n"), "--- END GUIDELINES ---"]
    if not parts:
        return "", 0, skipped
    return ("=== PROJECT GUIDELINES ===\n" + "\n".join(parts) + "\n=== END GUIDELINES ===\n", redactions,
            skipped)


def context_packet(question, files, base_dir, diff_text="", max_chars=400000):
    """Question plus context files (and optionally a change packet). Returns (text, stats)."""
    question, redactions = redact(question.strip())
    parts = ["=== QUESTION ===", question, "=== END QUESTION ==="]
    omitted, included = [], []
    for rel in files:
        full = rel if os.path.isabs(rel) else os.path.join(base_dir, rel)
        if sensitive(rel) or sensitive(os.path.realpath(full)):  # a link named notes.md may point at a .env file
            omitted.append((rel, "sensitive path"))
            continue
        # A relative path must stay inside the folder once links are resolved, a linked directory on the way included;
        # an absolute path is an explicit choice of the user.
        root = os.path.realpath(base_dir)
        inside = os.path.realpath(full).startswith(root + os.sep)
        if not os.path.isabs(rel) and not inside:
            omitted.append((rel, "resolves outside the folder"))
            continue
        if not os.path.isfile(full):
            raise PacketError("context file not found: %s" % rel)
        text, reason = read_text(full, follow_links=True)  # named explicitly by the user
        if text is None:
            omitted.append((rel, reason))
            continue
        # An absolute path is shown relative to the folder, or by its file name only: the rest of it names the
        # user's own directories.
        label = rel
        if os.path.isabs(rel):
            label = os.path.relpath(os.path.realpath(full), root) if inside else os.path.basename(rel)
        text, n = redact(text)
        shown, k = redact(label)
        redactions += n + k
        parts += ["", "--- CONTEXT FILE %s ---" % shown, numbered(text), "--- END CONTEXT FILE ---"]
        included.append(rel)
    if diff_text:
        parts += ["", diff_text.rstrip("\n")]
    line, n = not_included(omitted)
    redactions += n
    if line:
        parts += ["", line]
    text = "\n".join(parts) + "\n"
    if len(text) > max_chars:
        raise PacketError("the context is too large to send (%d characters, limit %d)" % (len(text), max_chars))
    return text, {"files": included, "omitted": omitted, "redactions": redactions, "chars": len(text)}
