# Prompt versions

The chat agent and the onboarding parser each have a versioned system prompt. The live text is a file in `app/prompts/`. Code asks the registry which version to send.

| Family | Active env | Allowlist env | Stable default |
| --- | --- | --- | --- |
| Chat | `CHAT_PROMPT_VERSION` | `CHAT_PROMPT_ALLOWLIST` | `chat-v1` |
| Onboarding parse | `ONBOARDING_PROMPT_VERSION` | `ONBOARDING_PROMPT_ALLOWLIST` | `onboarding-v1` |

An empty version env uses the latest **stable** version. `chat-v2` is registered and is not stable, so production stays on `chat-v1` until you opt in. `chat-v2` is the wording that does not blank days before the anchor.

## Add a version

1. Copy the current file, for example `app/prompts/chat/v1.md`, to `app/prompts/chat/v3.md`.
2. Edit only the new file. Do not change a version that has already shipped. `chat-v1` must stay byte for byte.
3. Register it in `app/prompts/registry.py` with an id (`chat-v3`), a one-line changelog, a created date, and `stable=False`.
4. If a tool description has to change with the prompt, add that text as a file and set `tool_path` on the new version only.
5. Add a test that the new id loads, and that the default is still the previous stable version.

## Test it on test accounts

Set the allowlist on the service you use for those accounts. Only the listed user ids leave the active version:

```
CHAT_PROMPT_ALLOWLIST=<user-uuid>:chat-v2
```

Other accounts keep the stable prompt. An unknown id in the allowlist is ignored and a warning is logged.

To try a version for every account on a staging service, set `CHAT_PROMPT_VERSION=chat-v2` there instead.

## Switch it live

When the new version is the one you want for everyone, either:

- set `CHAT_PROMPT_VERSION=chat-v2` on Render, or
- mark that version `stable=True` (and the previous one `stable=False` if it should no longer be the default) and deploy.

The env value wins over the stable default. Changing the env does not need a code deploy. Render restarts the service when the env changes.

## Roll back

Set `CHAT_PROMPT_VERSION=chat-v1` and save. The next process uses v1. Set the value back to empty to follow the stable default again.

An unknown value, such as a typo, logs a warning and uses the latest stable version.

## What gets recorded

Each model reply includes `prompt_version` on the chat response. Assistant messages store it in `metadata.prompt_version`. Proposals store it on `validation.prompt_version`.

A `prompt_version` column is optional. Until it exists, the id is logged and kept in that JSON. The insert is retried without the column if the database does not have it yet. The migration is in the pull request description, for the owner to run.
