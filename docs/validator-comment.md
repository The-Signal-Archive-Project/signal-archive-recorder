# Intake validator verdicts

## When a session is complete

The recorder uploads each session as **one PR, in several commits**: the first chunk opens the PR, each further chunk is its own commit, and the labels and `session.json` come last. An interrupted upload later resumes in the same PR. So **`session.json` appearing in the PR means the upload is complete.** The validator should wait for it, then check that every chunk listed in it is present.

The intake repository (`signal-archive-project/signal-archive-intake`) receives one pull request per recording session from Signal Archive Recorder. An automated validator checks each PR and reports back as a **PR comment** with a fenced block:

````markdown
Automated check for contributions/w9xyz/20261005T120307Z:

```signal-archive-validator
{"result": "pass", "validator_version": "1.0", "messages": []}
```
````

| Field | Type | Meaning |
|---|---|---|
| `result` | `"pass"` or `"fail"` | The verdict |
| `validator_version` | string | Which validator produced it |
| `messages` | list of strings | Human-readable reasons, mainly for `fail` |

How the recorder reads it (`upload/validator.py`):
- It reads the comments oldest first. **The newest valid block wins**, so a re-run can overturn an earlier verdict.
- `pass` puts the session in state `validated`, and `fail` puts it in `failed` with the messages shown to the contributor (`signal-archive-recorder status`).
- If there's no verdict, a merged PR counts as `validated` and a closed, unmerged PR counts as `failed`.
- Malformed blocks are ignored.
