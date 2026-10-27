# Milestone 1 — CPU-Only Development Environment

**Project:** Industrial RAG Copilot
**Milestone:** M1 — Foundation (GitHub Issue [#1](https://github.com/hafizshah101-code/industrial-rag-copilot/issues/1))
**Status:** Completed — Issue #1 closed
**Date:** 28 October 2026 (Malaysia time)
**Scope:** Windows laptop development environment; no RAG ingestion or model inference yet.

## Objective

Establish a reproducible, CPU-only Python development baseline with isolated dependencies, automated tests, lint/format checks, Git tracking, and cryptographically signed commits. The project is intended for learning AI engineering and building an auditable public portfolio.

## Hardware and Software Baseline

| Component | Recorded configuration |
| --- | --- |
| Laptop CPU | Intel Core i5-8365U |
| RAM | 16 GB |
| GPU | Intel UHD Graphics 620 (integrated); no discrete GPU required |
| Operating system | Windows x64 |
| Project Python | 3.12.10 (Python launcher also reported 3.14.7; project explicitly uses 3.12) |
| Git | 2.55.0.windows.3 |
| VS Code | 1.140.0 |
| WSL | WSL2 configured; `docker-desktop` default distribution; Docker runtime not validated in M1 |
| pip | 26.2.2 in `.venv` |
| pytest | 9.1.1 |
| Ruff | 0.17.3 |

The developer workstation uses `C:\Projects\industrial-rag-copilot` and a local `.venv` directory. Paths are examples, not mandatory project configuration.

## Repository Setup and Isolation

1. Reused an existing local folder without deleting its `.venv` or other work.
2. Initialized Git and configured the GitHub remote.
3. Fetched `origin/main` and switched the local branch to track it.
4. Resolved collisions with empty local `.gitkeep` placeholders by moving them to a backup directory before checkout.
5. Activated the existing Python 3.12 virtual environment; confirmed that `sys.executable` points to `.venv\Scripts\python.exe`.
6. Added development dependency version pins in `requirements-dev.txt` and `.python-version`.

### Reproduce the development checks

From a clean clone of the repository with Python 3.12 installed:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Use the virtual environment's Python executable directly when PowerShell execution policy prevents activation. If activation is desired, `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` changes policy only for the current session, subject to applicable organization policies.

> Reproducibility note: `requirements-dev.txt` pins the **direct** development tools (`pytest` and `ruff`), not their full transitive dependency graph. A lockfile and automated clean-clone CI validation can be added in a later milestone.

## Automated Smoke Tests

`tests/test_environment.py` verifies:

1. Interpreter version: Python 3.12.
2. Expected project directories: `app`, `data`, `docs`, and `tests`.
3. Isolation: Python executes inside a virtual environment (`sys.prefix != sys.base_prefix`).

### Recorded local results

| Check | Result |
| --- | --- |
| `python -m pytest -v` | **3 passed** (final recorded run: 0.04 seconds) |
| `python -m ruff check .` | **All checks passed** |
| `python -m ruff format --check .` | **6 files already formatted** |
| `git diff --cached --check` | No issues reported |
| `git status` after synchronizing `main` | Clean; up to date with `origin/main` |

These results are **reported from the laptop's terminal output**, not from a CI runner. Automated CI is scheduled for a later milestone.

## Troubleshooting Record

### 1. Existing folder prevented `git clone`

**Symptom:** `fatal: destination path ... already exists and is not an empty directory`.

**Resolution:** Safely initialized Git within the existing directory, fetched the remote, and backed up three empty `.gitkeep` placeholders that conflicted with checkout. No force checkout or destructive reset was used.

### 2. Virtual-environment activation blocked by PowerShell

**Symptom:** `Activate.ps1 cannot be loaded because running scripts is disabled on this system`.

**Resolution:** Temporarily changed execution policy only for the active PowerShell process, activated `.venv`, and verified that the Python interpreter path was inside the project virtual environment.

### 3. Ruff formatter panicked on a Python source file

**Symptom:** Ruff 0.17.3 reported `Annotation range ... beyond the end of buffer` while running `ruff format --check`.

**Evidence:** `Format-Hex` showed a UTF-8 byte-order mark (`EF BB BF`) at the beginning of `tests/test_environment.py`, which had been written using PowerShell.

**Resolution:** Re-encoded the source file as UTF-8 **without BOM**, then ran the Ruff formatter and all quality checks again. The panic disappeared, and the final checks passed. This sequence supports a BOM-related trigger for this case; it does not independently establish the upstream root cause in Ruff.

### 4. Local Git signature verification initially returned `N`

**Symptom:** `gpg.ssh.allowedSignersFile needs to be configured`.

**Resolution:** Configured a repository-specific SSH `allowed_signers` file. `git verify-commit HEAD` then reported `Good "git" signature`, and `%G?` returned `G`. The repository owner additionally confirmed a **Verified** badge for commit `5cf5147` on GitHub.

## Git Security and Review Workflow

- Generated a separate Ed25519 SSH signing key for this project.
- Registered its **public** key with GitHub as a signing key.
- Enabled Git SSH signing via repository-local configuration.
- Kept the private key, passphrase, local virtual environment, and vendor PDFs out of the repository.
- Used feature branches and pull requests instead of pushing feature work directly to `main`.
- Confirmed a Verified badge for the smoke-test commit. The dependency commit was SSH-signed during creation; its GitHub badge was not independently recorded in the conversation.

**Important distinction:** GitHub's **Verified** badge confirms a commit signature; it does not prove that tests passed or that a code change is safe.

## GitHub Evidence

- [Issue #1 — CPU-only development environment (closed)](https://github.com/hafizshah101-code/industrial-rag-copilot/issues/1)
- [PR #10 — Environment smoke tests (merged)](https://github.com/hafizshah101-code/industrial-rag-copilot/pull/10)
- [Verified smoke-test commit `5cf5147`](https://github.com/hafizshah101-code/industrial-rag-copilot/commit/5cf51476e6fe04cd5dc12602ec928f2acf4474d6)
- [PR #11 — Pin development dependencies (merged)](https://github.com/hafizshah101-code/industrial-rag-copilot/pull/11)
- [Dependency pinning commit `70b3583`](https://github.com/hafizshah101-code/industrial-rag-copilot/commit/70b358358373f7ccc96bf671073659ad6921b024)

## Lessons Learned

- Pin the project interpreter explicitly when multiple Python versions exist.
- Reuse existing project directories carefully; inspect untracked files before checkout.
- Distinguish application test failures from development-tool failures.
- Verify encoding and file bytes when a formatter crashes unexpectedly.
- Separate SSH **signing**, **local signature verification**, and GitHub's **Verified** status.
- Keep reproducible test evidence and changes traceable through pull requests.

## Boundary and Next Step

**M1 is complete.** No PDF parser, embedding model, vector store, LLM, or RAG retrieval pipeline was implemented in this milestone. The next planned task is [Issue #2 — PDF inspection and page-provenance extraction](https://github.com/hafizshah101-code/industrial-rag-copilot/issues/2); it has not been started as part of this documentation.
