# Debugging Request/Response Packets

This document explains how to enable detailed request/response logging for debugging issues with the Claude Code Proxy.

## Environment Variables

To enable detailed request/response packet logging, set the following environment variables:

```bash
# Enable detailed request/response logging
DEBUG_REQUESTS=true

# Set log level to DEBUG to see all debug messages
LOG_LEVEL=DEBUG
```

## What Gets Logged

When `DEBUG_REQUESTS=true` is set, the proxy will log:

1. **Incoming Claude Requests**: The original request from Claude Code in Claude's API format
2. **Converted OpenAI Requests**: The request after conversion to OpenAI's format
3. **Outgoing OpenAI Requests**: The actual request sent to the OpenAI API
4. **Incoming OpenAI Responses**: The response received from the OpenAI API
5. **Outgoing Claude Responses**: The final response sent back to Claude Code in Claude's format

## Log Format

Each packet is logged with clear delimiters:

```
=== INCOMING CLAUDE REQUEST ===
Claude request: {
  "model": "claude-3-sonnet-20241022",
  "messages": [...],
  ...
}

=== CONVERTED OPENAI REQUEST ===
Converted to OpenAI: {
  "model": "gpt-4o",
  "messages": [...],
  ...
}

=== OUTGOING REQUEST (abc-123-def) ===
Request to OpenAI: {
  "model": "gpt-4o",
  "messages": [...],
  ...
}

=== INCOMING RESPONSE (abc-123-def) ===
Response from OpenAI: {
  "id": "chatcmpl-...",
  "choices": [...],
  ...
}

=== OUTGOING CLAUDE RESPONSE ===
Claude response: {
  "id": "msg_...",
  "content": [...],
  ...
}
```

## Example Usage

### Docker Compose

```yaml
version: '3.8'
services:
  claude-proxy:
    build: .
    ports:
      - "8082:8082"
    environment:
      - OPENAI_API_KEY=your_openai_key_here
      - ANTHROPIC_API_KEY=your_anthropic_key_here
      - DEBUG_REQUESTS=true
      - LOG_LEVEL=DEBUG
```

### Shell Export

```bash
export DEBUG_REQUESTS=true
export LOG_LEVEL=DEBUG
python -m src.main
```

### Docker Run

```bash
docker run -p 8082:8082 \
  -e OPENAI_API_KEY=your_openai_key_here \
  -e ANTHROPIC_API_KEY=your_anthropic_key_here \
  -e DEBUG_REQUESTS=true \
  -e LOG_LEVEL=DEBUG \
  claude-code-proxy
```

## Streaming Requests

For streaming requests, the proxy will log:
- The initial streaming request
- Individual chunks (only chunks with actual content to avoid spam)
- The stream completion

## Security Considerations

⚠️ **WARNING**: Debug logging will output API keys, authentication tokens, and potentially sensitive data from conversations. Only enable debug logging in development/testing environments and ensure logs are properly secured.

## Performance Impact

Debug logging can significantly increase log volume and may impact performance. Only enable it when actively debugging issues.

## Common Debugging Scenarios

### 1. "thinking block" Format Errors

The error you're seeing suggests the model expects a `thinking` block format. Check the logs for:
- The incoming Claude request format
- The converted OpenAI request
- The OpenAI response format

### 2. Model Configuration Issues

Check the logs to verify:
- Which model is being requested
- Which model is actually being used after conversion
- Any model-specific parameters being passed

### 3. Message Format Issues

Look for:
- How messages are being converted between Claude and OpenAI formats
- Content blocks and their types
- Tool use formatting

## Saving Logs to File

To save debug logs to a file for analysis:

```bash
export DEBUG_REQUESTS=true
export LOG_LEVEL=DEBUG
python -m src.main 2>&1 | tee debug.log
```

Or redirect only to file:

```bash
export DEBUG_REQUESTS=true
export LOG_LEVEL=DEBUG
python -m src.main > debug.log 2>&1
```