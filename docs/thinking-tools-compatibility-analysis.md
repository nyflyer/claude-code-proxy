# Thinking + Tools Compatibility Issue: Root Cause Analysis

## Problem Summary

The Claude Code proxy experiences compatibility issues when both **thinking mode** and **tool calls** are enabled simultaneously. This results in API errors from AWS Bedrock Claude, specifically:

```
BedrockException - The model returned the following errors: 
messages.1.content.0.type: Expected `thinking` or `redacted_thinking`, but found `text`. 
When `thinking` is enabled, a final `assistant` message must start with a thinking block.
```

## Architecture Context

The request flow is:
```
Claude Code → Proxy → LiteLLM (OpenAI API) → AWS Bedrock Claude → LiteLLM → Proxy → Claude Code
```

The proxy converts Claude's native format to OpenAI format for LiteLLM compatibility, which then converts to AWS Bedrock's format.

## Detailed Investigation

### Working Scenario: Text-Only Thinking ✅

When a request contains **only thinking** (no tools):

**Request to Proxy:**
```json
{
  "model": "claude-3-5-sonnet",
  "messages": [{"role": "user", "content": "Think through this problem"}],
  "thinking": {"enabled": true}
}
```

**Proxy → LiteLLM (OpenAI format):**
```json
{
  "model": "claude-sonnet-4",
  "messages": [{"role": "user", "content": "Think through this problem"}],
  "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 4096}}
}
```

**LiteLLM → AWS Bedrock (Claude format):**
```json
{
  "messages": [
    {
      "role": "assistant",
      "content": [
        {"type": "thinking", "thinking": "Let me think through this..."}
      ]
    }
  ],
  "thinking": {"enabled": true}
}
```

**Result:** ✅ **Works perfectly** - AWS Bedrock receives proper thinking blocks.

### Working Scenario: Tools-Only (No Thinking) ✅

When a request contains **only tools** (no thinking):

**Request to Proxy:**
```json
{
  "model": "claude-3-5-sonnet",
  "messages": [{"role": "user", "content": "Read a file"}],
  "tools": [{"name": "read_file", ...}]
}
```

**Proxy → LiteLLM (OpenAI format):**
```json
{
  "model": "claude-sonnet-4", 
  "messages": [
    {
      "role": "assistant",
      "content": "I'll help you read that file.",
      "tool_calls": [{"id": "call_123", "type": "function", "function": {...}}]
    },
    {
      "role": "tool",
      "tool_call_id": "call_123", 
      "content": "file contents"
    }
  ],
  "tools": [...]
}
```

**LiteLLM → AWS Bedrock (Claude format):**
```json
{
  "messages": [
    {
      "role": "assistant",
      "content": [
        {"type": "text", "text": "I'll help you read that file."},
        {"type": "tool_use", "id": "call_123", "name": "read_file", "input": {...}}
      ]
    },
    {
      "role": "user",
      "content": [
        {"type": "tool_result", "tool_use_id": "call_123", "content": "file contents"}
      ]
    }
  ]
}
```

**Result:** ✅ **Works perfectly** - Standard tool calling flow.

### Failing Scenario: Thinking + Tools ❌

When a request contains **both thinking and tools**:

**Request to Proxy:**
```json
{
  "model": "claude-3-5-sonnet",
  "messages": [{"role": "user", "content": "Think about this and use tools"}],
  "tools": [{"name": "read_file", ...}],
  "thinking": {"enabled": true}
}
```

**Proxy → LiteLLM (OpenAI format):**
```json
{
  "model": "claude-sonnet-4",
  "messages": [
    {
      "role": "assistant",
      "content": "I need to think about this and read a file.",
      "tool_calls": [{"id": "call_123", "type": "function", "function": {...}}]
    }
  ],
  "tools": [...],
  "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 4096}}
}
```

**LiteLLM → AWS Bedrock (Attempted conversion):**
```json
{
  "messages": [
    {
      "role": "assistant", 
      "content": [
        {"type": "text", "text": "I need to think about this and read a file."},  // ❌ PROBLEM!
        {"type": "tool_use", "id": "call_123", "name": "read_file", "input": {...}}
      ]
    }
  ],
  "thinking": {"enabled": true}  // ← This tells Bedrock to expect thinking blocks
}
```

**AWS Bedrock Response:** ❌ **REJECTS** with error:
```
Expected `thinking` or `redacted_thinking`, but found `text`. 
When `thinking` is enabled, a final `assistant` message must start with a thinking block.
```

## Root Cause Analysis

### The Core Issue

**LiteLLM's thinking mode conversion capabilities are limited:**

1. **Simple text messages**: LiteLLM can successfully convert to `{"type": "thinking"}` blocks
2. **Complex messages with tool_calls**: LiteLLM **cannot** properly convert to thinking format
3. **Result**: Mixed format sent to AWS Bedrock (thinking enabled but text blocks instead of thinking blocks)

### Why This Happens

1. **LiteLLM sees `extra_body.thinking`** → "I need to enable thinking mode for AWS Bedrock"
2. **LiteLLM sees complex OpenAI message with `tool_calls`** → "I need to convert this to Claude tool format"
3. **LiteLLM's limitation**: It converts the text content to `{"type": "text"}` blocks, not `{"type": "thinking"}` blocks
4. **AWS Bedrock receives**: Thinking mode enabled + text blocks (expects thinking blocks)
5. **AWS Bedrock rejects**: Format mismatch

### Why Text-Only Works vs Tools Fail

| Scenario | LiteLLM Input | LiteLLM Conversion Capability | Result |
|----------|---------------|------------------------------|---------|
| **Text-only + thinking** | Simple text message | ✅ Can convert to thinking blocks | ✅ Works |
| **Tools + thinking** | Complex tool_calls + text | ❌ Cannot convert properly | ❌ Fails |
| **Tools only** | Complex tool_calls + text | ✅ Standard conversion (no thinking) | ✅ Works |

## Investigation Evidence

### Test Results

**Text-only thinking request produces:**
```json
{
  "extra_body": {"thinking": {"type": "enabled"}},
  "messages": [{"role": "user", "content": "simple text"}]
}
```
→ **Works in practice**

**Tools + thinking request (when forced to bypass Option 2) produces:**
```json
{
  "extra_body": {"thinking": {"type": "enabled"}},
  "tools": [...],
  "messages": [
    {
      "role": "assistant",
      "content": "text",
      "tool_calls": [...]
    }
  ]
}
```
→ **Fails with the exact error we observed**

### LiteLLM Documentation Findings

From LiteLLM documentation on Bedrock thinking mode:
- Supports thinking mode for Claude 3.7 Sonnet and Deepseek R1
- Returns `reasoning_content` and `thinking_blocks` 
- Requires `signature` field for subsequent calls with tool calling
- Uses `reasoning_effort` parameter or `anthropic-beta` headers

**Key Gap**: Documentation doesn't clearly specify how LiteLLM handles the combination of `extra_body.thinking` + `tool_calls` in the same request.

## Current Solution: Option 2

### Implementation
Modified `should_enable_thinking()` in `src/core/model_manager.py`:

```python
def should_enable_thinking(self, claude_request) -> bool:
    # Disable thinking if tools are present (LiteLLM format incompatibility)
    if claude_request.tools:
        logger.info("Thinking disabled due to tool presence (LiteLLM compatibility)")
        return False
    # ... rest of existing logic
```

### Option 2 Results

✅ **Advantages:**
- Simple 2-line fix
- Tools work reliably 
- Thinking works for non-tool requests
- No API errors

❌ **Disadvantages:**
- **Major limitation**: Since Claude Code uses tools for almost everything (Read, Write, Edit, Bash, etc.), thinking is disabled for ~95% of interactions
- Users lose thinking capability they specifically requested
- Thinking becomes essentially unusable in practice

## Alternative Solutions Considered

### Option 4: Sequential Tool/Thinking Approach

**Concept**: Make multiple API calls in sequence:
1. **Thinking call** → Get reasoning without tools
2. **Tool execution** → Parse thinking output and execute tools locally  
3. **Final call** → Process tool results with thinking enabled

**Pros**: Both features work fully
**Cons**: 2-3x cost/latency, complex implementation (~5-7 weeks development)

**Status**: Detailed specification saved in `docs/thinking-tools-sequential-approach.md`

## Potential Future Solutions

### 1. Enhanced Message Formatting
**Concept**: Find a way to format our OpenAI messages so LiteLLM can properly convert them to thinking blocks.

**Approach**: 
- Research LiteLLM's exact conversion logic
- Experiment with different message formats
- Test if specific content patterns trigger proper thinking conversion

### 2. Selective Thinking
**Concept**: Enable thinking only for messages that LiteLLM can handle properly.

**Approach**:
- Enable thinking for simple text responses
- Disable thinking only for complex tool interactions
- More nuanced than blanket Option 2 approach

### 3. Pre-Thinking Approach  
**Concept**: Do thinking before tool execution, not during.

**Approach**:
- Thinking-enabled call to plan tool usage
- Separate tools-only call to execute
- Simpler version of Option 4

### 4. LiteLLM Enhancement
**Concept**: Work with LiteLLM maintainers to improve thinking+tools conversion.

**Approach**:
- Report the specific conversion limitation
- Contribute fixes to LiteLLM project
- Wait for upstream improvements

### 5. Format Detection and Routing
**Concept**: Automatically detect when thinking+tools combination will fail and route accordingly.

**Approach**:
- Attempt thinking+tools combination
- On failure, automatically retry with tools-only
- Transparent fallback for users

## Recommendations

### Short Term (Current)
Continue with **Option 2** as it provides stability, but with awareness of its significant limitation for Claude Code's tool-heavy usage patterns.

### Medium Term
Investigate **Enhanced Message Formatting** (#1) and **Selective Thinking** (#2) as they could provide better balance between functionality and complexity.

### Long Term  
Consider **Option 4 (Sequential Approach)** if thinking becomes critical for user workflows and the cost/complexity trade-offs become acceptable.

### Monitor
Watch for LiteLLM updates that might improve thinking+tools compatibility, potentially resolving the issue upstream.

## Conclusion

The thinking+tools compatibility issue stems from **LiteLLM's limited conversion capabilities** when handling complex OpenAI messages with both thinking enablement and tool calls. While text-only thinking works perfectly, the combination breaks due to format expectations at the AWS Bedrock level.

Option 2 provides a pragmatic short-term solution but significantly limits thinking functionality in practice. Future solutions should focus on either improving the message format conversion or implementing more sophisticated request orchestration to preserve both features.

The root cause is well-understood, enabling targeted solutions that address the specific LiteLLM conversion limitation rather than broader architectural changes.