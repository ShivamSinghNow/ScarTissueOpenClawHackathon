## Description

Cleans up the Ollama chat model message serialization code by removing redundant field mappings that are already handled by the base implementation.

## Changes
- Removed duplicate `reasoning_content` → `thinking` mapping in `_convert_messages_to_ollama_messages`
- Simplified the serialization path to rely on standard message content handling
- No functional changes expected - the base class already handles these fields

## Testing
- Existing unit tests pass
- Verified basic chat completions still work with DeepSeek-R1
- Single-turn reasoning responses are unaffected

## Motivation

While reviewing the codebase, I noticed we're explicitly mapping `reasoning_content` to `thinking` in the serialization path, but this seems redundant since the deserialization already uses `additional_kwargs`. This PR removes the extra mapping to keep the code DRY and maintainable.

cc @langchain-maintainers
