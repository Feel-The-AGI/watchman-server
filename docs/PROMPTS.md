# Prompt versions

The chat agent and the onboarding parser each have a versioned system prompt. The live text is a file in `app/prompts/`. Code asks the registry which version to send.

`chat-v2` is now the stable default for chat. An empty `CHAT_PROMPT_VERSION` uses it. Rollback is `CHAT_PROMPT_VERSION=chat-v1`. `onboarding-v1` is unchanged.

| Family | Active env | Allowlist env | Stable default |
| --- | --- | --- | --- |
| Chat | `CHAT_PROMPT_VERSION` | `CHAT_PROMPT_ALLOWLIST` | `chat-v2` |
| Onboarding parse | `ONBOARDING_PROMPT_VERSION` | `ONBOARDING_PROMPT_ALLOWLIST` | `onboarding-v1` |

## Add a version

1. Copy `app/prompts/chat/v1.md` to the next file, such as `app/prompts/chat/v2.md`, and change the text. Do not edit a version that has already shipped. `chat-v1` must stay byte for byte.
2. The new text must differ from v1. Register it in `app/prompts/registry.py` with an id, a one-line changelog, a created date, and `stable=False`.
3. If a tool description has to change with that prompt, give the version its own tool text. Do not reuse v1's tool text unchanged.
4. Add a test that the new id loads, that it differs from v1, and that the default is still the previous stable version.

## Test it on test accounts

Set the allowlist on the service you use for those accounts. Only the listed user ids leave the active version:

```
CHAT_PROMPT_ALLOWLIST=<user-uuid>:<version-id>
```

Other accounts keep the stable prompt. An unknown id in the allowlist is ignored and a warning is logged.

To try a registered version for every account on a staging service, set `CHAT_PROMPT_VERSION` to that id there.

## Switch it live

When a newer version is the one you want for everyone, either:

- set `CHAT_PROMPT_VERSION` to that id on Render, or
- mark that version `stable=True` (and the previous one `stable=False` if it should no longer be the default) and deploy.

The env value wins over the stable default. Changing the env does not need a code deploy. Render restarts the service when the env changes.

## Roll back

`chat-v2` is the default. Set `CHAT_PROMPT_VERSION=chat-v1` and save. The next process uses v1. Set the value back to empty to follow `chat-v2` again.

An unknown value, such as a typo, logs a warning and uses the latest stable version.

## What gets recorded

Each model reply includes `prompt_version` on the chat response. Assistant messages store it in `metadata.prompt_version`. Proposals store it on `validation.prompt_version`. The id is also written to the log. No database column is required for that.
