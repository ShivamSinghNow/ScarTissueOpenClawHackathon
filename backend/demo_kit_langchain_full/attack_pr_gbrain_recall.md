# Attack PR: GBrain cross-session recall

Companion to `attack_pr_gbrain_recall.diff`. This is **PR #2** in the SCA-9 demo
flow — the one that proves ScarTissue can recall a pattern it learned from a
*different* PR earlier in the session, via GBrain's semantic graph rather than
ChromaDB's surface-similarity search.

## PR metadata

- **Title:** `refactor(openai): drop redundant tool-schema copies in _resolve_tool_choice`
- **File touched:** `libs/partners/openai/langchain_openai/chat_models/base.py`
- **Function:** `_resolve_tool_choice`
- **Diff size:** 2 deletions, comment edit

## PR description (what a sloppy contributor would write)

> Cleanup pass: `_resolve_tool_choice` was making two defensive copies of every
> tool dict — a shallow `[dict(t) for t in tools]` and a `copy.deepcopy` of each
> `parameters` block — but the resulting list is freshly serialized into the
> request body on every call, so nothing downstream can observe the mutations.
> Dropping both copies removes an allocation hot-spot we saw on big tool sets
> (50+ tools, deeply nested JSON schemas). No behavior change in tests.

## The rhyme — what this PR actually does

It removes the only thing standing between `_resolve_tool_choice` and silent
mutation of caller-owned tool schemas. After the diff:

1. `parameters.setdefault("additionalProperties", False)` writes directly into
   the caller's nested `parameters` dict.
2. `_drop_unsupported_keys(parameters)` mutates the same dict in place.
3. `tool["function"] = fn` and `tools[idx] = tool` write back into the caller's
   list.

Any caller that holds the same `tools` list across invocations (e.g. an Agent
that constructs it once at init and reuses it on every `.invoke()`) will see
its schemas progressively corrupted on the **second** call — `oneOf` entries
silently dropped, `additionalProperties: False` injected into objects the
caller never opted into, etc.

## Source of the scar tissue (PR #1)

| | |
|---|---|
| Source incident SHA | `bc21045ee054b233aa6c4f65653b26a0a7ff340f` |
| Source file | `libs/partners/ollama/langchain_ollama/chat_models.py` |
| Source function | `_convert_messages_to_ollama_messages` |
| Source fix | Added `messages = list(messages)` before the mutation loop |

The two PRs share **zero** surface tokens — different package, different file
path, different variable name (`tools` vs `messages`), different object type
(`dict` vs `BaseMessage`), different domain vocabulary. They share one
**abstract** pattern: *converter function removes defensive copy of
caller-owned mutable input before an in-place mutation loop.*

## Expected ScarTissue warning

```
⚠️  HIGH — confidence 0.85
  pr_file       : libs/partners/openai/langchain_openai/chat_models/base.py
  matched       : bc21045e (live_warning · learned from PR #1, 14s ago)
  source        : gbrain
  explanation   : This PR removes the defensive shallow + deep copy of caller-
                  owned tool schemas before _resolve_tool_choice mutates them
                  in place, rhyming with bc21045e where the same protective
                  copy was removed from ollama's message converter and caused
                  silent caller-list corruption.
  proposed_fix  : Restore `tools = [dict(t) for t in tools]` and
                  `parameters = copy.deepcopy(parameters)` before the loop, or
                  rewrite the function to construct a fresh output list
                  without writing back into `tools[idx]`.
```

The critical UI element is the `live_warning` tag with `source: gbrain` and the
"learned … ago" timestamp — that is the proof-point the judges are watching
for. A vanilla ChromaDB hit on this diff would tag `source: git_history` and
cite a stale incident; only GBrain's semantic-cause graph produces the
"learned from PR #1" attribution.

## Why ChromaDB alone will not catch this

`scar_index.ScarIndex` embeds incidents as
`commit_message[:500] + files + fix_diff[:1500]` with `all-MiniLM-L6-v2`. The
bc21045 incident's embedding text is dominated by tokens like `ollama
messages convert v1 content mutate caller list shallow copy`. This PR's diff
contains `openai tools function parameters strict additionalProperties oneOf
schema deepcopy`. Cosine similarity in MiniLM-L6 space puts these two roughly
**0.3–0.4** apart — well outside the top-5 the agent inspects.

## Demo timeline

1. **t=0s** — Apply `attacks/bc21045.../attack.patch` to a branch, push, open
   PR. ScarTissue reviews it, emits HIGH warning citing bc21045 from git
   history. `learn_from_incident` writes the abstract pattern node into
   GBrain (`source: git_history`).
2. **t≈14s** — Apply `attack_pr_gbrain_recall.diff` to a different branch,
   push, open PR. ScarTissue reviews it; the ChromaDB hit for bc21045 is
   *not* in the top-5 (different vocabulary), but GBrain's
   `search_scar_tissue` augmentation returns the pattern node linked to
   bc21045 with a `learned_at` timestamp from step 1.
3. The warning UI renders with the pulsing-green `live_warning` badge.

## Notes for the operator

- This diff does **not** need to apply cleanly to a real LangChain checkout
  for the demo to fire — ScarTissue parses hunks via `unidiff` and the
  reviewer agent reasons on the diff text, not on a working tree.
- If you want to push a real GitHub PR for the demo, target a recent
  langchain `main` and let the diff fuzz onto whatever the current line
  numbers of `_resolve_tool_choice` are; the hunk header is generous enough
  to slide.
- Coordinate with Track A's GBrain `learn_from_incident` hook to confirm the
  pattern key it writes for bc21045 matches what its query path would
  produce for this PR's diff. The pattern shape we are betting on is
  approximately: *"converter function strips defensive copy of caller-owned
  mutable structured input before in-place mutation loop"*.
