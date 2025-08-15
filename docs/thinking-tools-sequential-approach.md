# Option 4: Sequential Tool/Thinking Approach

## Overview

This document outlines Option 4 for combining thinking and tool functionality - a sequential approach that makes multiple API calls to preserve both features fully.

## Current Problem

When both thinking and tools are enabled:
- **Thinking mode**: AWS Bedrock Claude expects native Claude format with `{"type": "thinking"}` blocks
- **Tool mode**: LiteLLM requires OpenAI format with `tool_calls` and `role: "tool"` messages  
- **LiteLLM limitation**: Cannot pass through native Claude format when thinking is enabled
- **Result**: Format incompatibility causes API errors

## Sequential Approach Solution

Instead of one API call with mixed format, use multiple sequential calls:

1. **Thinking Call** → Get reasoning without tools
2. **Tool Execution** → Parse thinking output and execute tools locally  
3. **Final Call** → Process tool results with thinking enabled

## Architecture

### High-Level Flow

```
User Request (thinking + tools)
    ↓
Orchestrator splits request
    ↓
┌─────────────────────────────────────────────────────────────┐
│  Call 1: Thinking Only                                      │
│  ┌─────────────────────┐    ┌──────────────────────────┐   │
│  │ Claude Request      │ → │ Claude Response           │   │
│  │ - thinking: enabled │    │ - reasoning text         │   │
│  │ - tools: none       │    │ - tool intentions        │   │
│  └─────────────────────┘    └──────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│  Local Tool Execution                                       │
│  ┌─────────────────────┐    ┌──────────────────────────┐   │
│  │ Parse thinking      │ → │ Execute tools locally     │   │
│  │ Extract intentions  │    │ Collect results          │   │
│  └─────────────────────┘    └──────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│  Call 2: Final Response                                     │
│  ┌─────────────────────┐    ┌──────────────────────────┐   │
│  │ Claude Request      │ → │ Claude Response           │   │
│  │ - thinking: enabled │    │ - final answer           │   │
│  │ - context: results  │    │ - incorporates tools     │   │
│  └─────────────────────┘    └──────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
    ↓
Final Response to User
```

## Implementation Details

### 1. New Orchestration Service

```python
class ThinkingToolOrchestrator:
    def __init__(self, claude_client):
        self.claude_client = claude_client
        self.tool_parser = ToolIntentionParser()
        self.tool_executor = LocalToolExecutor()
    
    async def handle_request(self, claude_request):
        """Main entry point for requests"""
        if not self._needs_orchestration(claude_request):
            # Simple case: single call
            return await self.claude_client.call(claude_request)
            
        # Complex case: orchestrated sequence
        return await self._orchestrated_sequence(claude_request)
    
    def _needs_orchestration(self, request):
        """Check if request needs orchestration"""
        return (request.tools and 
                request.thinking and 
                request.thinking.enabled)
    
    async def _orchestrated_sequence(self, claude_request):
        """Execute the 3-step orchestrated sequence"""
        # Step 1: Thinking call
        thinking_response = await self._thinking_call(claude_request)
        
        # Step 2: Tool execution  
        tool_results = await self._execute_tools(thinking_response)
        
        # Step 3: Final call
        final_response = await self._final_call(
            claude_request, thinking_response, tool_results
        )
        
        return final_response
```

### 2. Request Transformation

```python
def _create_thinking_request(self, original_request):
    """Create thinking-only request"""
    thinking_prompt = (
        "Think through this step by step. If you need to use tools, "
        "describe exactly what you would do but don't actually call them yet. "
        "Be specific about tool names and parameters you would use."
    )
    
    return ClaudeMessagesRequest(
        model=original_request.model,
        messages=original_request.messages + [
            {"role": "user", "content": thinking_prompt}
        ],
        thinking={"enabled": True},
        tools=None,  # Remove tools for thinking call
        max_tokens=original_request.max_tokens // 2,
        temperature=original_request.temperature,
        top_p=original_request.top_p
    )

def _create_final_request(self, original_request, thinking_response, tool_results):
    """Create final request with tool context"""
    context_prompt = (
        f"Based on your thinking: {thinking_response.content}\n\n"
        f"I've executed the tools you suggested. Here are the results:\n"
        f"{self._format_tool_results(tool_results)}\n\n"
        f"Please provide your final response to the original question."
    )
    
    return ClaudeMessagesRequest(
        model=original_request.model,
        messages=original_request.messages + [
            {"role": "assistant", "content": thinking_response.content},
            {"role": "user", "content": context_prompt}
        ],
        thinking={"enabled": True},  # Can still think in final call
        tools=None,  # No more tools needed
        max_tokens=original_request.max_tokens // 2,
        temperature=original_request.temperature,
        top_p=original_request.top_p
    )
```

### 3. Tool Intention Parser

```python
class ToolIntentionParser:
    def extract_tool_calls(self, thinking_text):
        """Extract tool intentions from thinking text"""
        tool_calls = []
        
        # Define patterns for each tool type
        patterns = {
            'read_file': r"read_file\(['\"]([^'\"]+)['\"]\)",
            'write_file': r"write_file\(['\"]([^'\"]+)['\"],\s*['\"]([^'\"]*?)['\"]\)",
            'bash': r"bash\(['\"]([^'\"]+)['\"]\)",
            'grep': r"grep\(['\"]([^'\"]+)['\"](?:,\s*['\"]([^'\"]+)['\"]))?\)",
            'edit': r"edit\(['\"]([^'\"]+)['\"],\s*['\"]([^'\"]*?)['\"],\s*['\"]([^'\"]*?)['\"]\)"
        }
        
        for tool_name, pattern in patterns.items():
            matches = re.findall(pattern, thinking_text, re.DOTALL)
            for match in matches:
                if tool_name == 'read_file':
                    tool_calls.append({
                        "tool": tool_name,
                        "args": {"file_path": match}
                    })
                elif tool_name == 'write_file':
                    tool_calls.append({
                        "tool": tool_name, 
                        "args": {"file_path": match[0], "content": match[1]}
                    })
                elif tool_name == 'bash':
                    tool_calls.append({
                        "tool": tool_name,
                        "args": {"command": match}
                    })
                elif tool_name == 'grep':
                    args = {"pattern": match[0]}
                    if len(match) > 1 and match[1]:
                        args["path"] = match[1]
                    tool_calls.append({
                        "tool": tool_name,
                        "args": args
                    })
                elif tool_name == 'edit':
                    tool_calls.append({
                        "tool": tool_name,
                        "args": {
                            "file_path": match[0],
                            "old_string": match[1], 
                            "new_string": match[2]
                        }
                    })
        
        return tool_calls
    
    def _sanitize_extracted_content(self, content):
        """Clean up extracted content from regex matches"""
        # Remove common parsing artifacts
        content = content.strip()
        content = content.replace('\\n', '\n')
        content = content.replace('\\t', '\t')
        return content
```

### 4. Local Tool Executor

```python
class LocalToolExecutor:
    async def execute_tools(self, tool_calls):
        """Execute the extracted tool calls locally"""
        results = []
        
        for tool_call in tool_calls:
            try:
                result = await self._execute_single_tool(tool_call)
                results.append({
                    "tool": tool_call["tool"],
                    "args": tool_call["args"],
                    "result": result,
                    "success": True
                })
            except Exception as e:
                results.append({
                    "tool": tool_call["tool"],
                    "args": tool_call["args"], 
                    "error": str(e),
                    "success": False
                })
        
        return results
    
    async def _execute_single_tool(self, tool_call):
        """Execute a single tool call"""
        tool_name = tool_call["tool"]
        args = tool_call["args"]
        
        if tool_name == "read_file":
            return await self._read_file(args["file_path"])
        elif tool_name == "write_file":
            return await self._write_file(args["file_path"], args["content"])
        elif tool_name == "bash":
            return await self._bash(args["command"])
        elif tool_name == "grep":
            return await self._grep(args["pattern"], args.get("path"))
        elif tool_name == "edit":
            return await self._edit(
                args["file_path"], args["old_string"], args["new_string"]
            )
        else:
            raise ValueError(f"Unknown tool: {tool_name}")
    
    # Tool implementation methods...
    async def _read_file(self, file_path):
        with open(file_path, 'r') as f:
            return f.read()
    
    async def _write_file(self, file_path, content):
        with open(file_path, 'w') as f:
            f.write(content)
        return f"Successfully wrote {len(content)} characters to {file_path}"
    
    # ... etc for other tools
```

### 5. Integration Points

```python
# In main request handler
class RequestHandler:
    def __init__(self):
        self.orchestrator = ThinkingToolOrchestrator(claude_client)
    
    async def handle_messages_request(self, request):
        # Use orchestrator instead of direct Claude client
        return await self.orchestrator.handle_request(request)
```

## Example Request Flow

### Original Request
```json
POST /v1/messages
{
  "model": "claude-3-5-sonnet-20241022",
  "messages": [
    {"role": "user", "content": "Read the README.md file and create a summary"}
  ],
  "tools": [
    {"name": "read_file", "description": "Read file contents", ...}
  ],
  "thinking": {"enabled": True},
  "max_tokens": 2000
}
```

### Internal Call Sequence

#### Call 1 (Thinking)
```json
{
  "model": "claude-3-5-sonnet-20241022",
  "messages": [
    {"role": "user", "content": "Read the README.md file and create a summary"},
    {"role": "user", "content": "Think through this step by step. If you need to use tools, describe exactly what you would do but don't actually call them yet."}
  ],
  "thinking": {"enabled": true},
  "max_tokens": 1000
}
```

**Response:**
```
I need to read the README.md file first to understand its contents, then create a summary. 

To do this, I should use read_file('README.md') to get the file contents, then analyze the content and create a concise summary highlighting the key points.
```

#### Tool Execution (Local)
Parser extracts: `read_file('README.md')`
Executor runs: `read_file('README.md')` → returns file contents

#### Call 2 (Final)
```json
{
  "model": "claude-3-5-sonnet-20241022", 
  "messages": [
    {"role": "user", "content": "Read the README.md file and create a summary"},
    {"role": "assistant", "content": "I need to read the README.md file first..."},
    {"role": "user", "content": "I've executed the tools you suggested. Here are the results:\n\nread_file('README.md'): [file contents]\n\nPlease provide your final response."}
  ],
  "thinking": {"enabled": true},
  "max_tokens": 1000
}
```

**Final Response:** Complete summary based on file contents

## Pros and Cons

### Advantages
✅ **Full feature preservation**: Both thinking and tools work completely  
✅ **Rich reasoning**: Claude can think about tool usage before execution  
✅ **No format conflicts**: Each call uses appropriate format  
✅ **Flexible tool combinations**: Can execute multiple tools based on thinking  
✅ **Error isolation**: Tool failures don't break thinking  
✅ **Context preservation**: Full conversation context maintained  

### Disadvantages  
❌ **Multiple API calls**: 2-3x cost and latency  
❌ **Complex parsing**: NLP required to extract tool intentions  
❌ **State management**: Complex orchestration logic  
❌ **Error handling**: Failures across multiple call boundaries  
❌ **Streaming complexity**: Cannot stream until final call completes  
❌ **Token management**: Need to split budgets across calls  
❌ **Parsing reliability**: Regex/NLP parsing may miss or misinterpret intentions  

## Implementation Complexity

### Development Effort Estimate
- **Core orchestration**: 1-2 weeks
- **Tool intention parsing**: 1 week  
- **Local tool execution**: 1 week
- **Error handling & edge cases**: 1 week
- **Testing & debugging**: 1-2 weeks
- **Streaming support**: 1 week (optional)

**Total: 5-7 weeks** vs **2 lines for Option 2**

### Maintenance Overhead
- **Tool parsing updates**: Each new tool needs parsing patterns
- **Error scenarios**: Complex multi-call failure modes
- **Performance monitoring**: Track costs across call sequences
- **Debugging complexity**: Issues span multiple API calls

## Alternative Considerations

### Hybrid Approach
Could implement both:
- **Option 2**: Default behavior (thinking disabled with tools)
- **Option 4**: Optional flag (`"enhanced_tool_thinking": true`)

Users could choose based on their needs:
- **Fast & reliable**: Use Option 2
- **Full featured**: Use Option 4 with acceptance of complexity/cost

### Future LiteLLM Enhancement
If LiteLLM eventually supports Claude native format passthrough, Option 4 becomes unnecessary. Monitor LiteLLM roadmap for:
- Native Claude format support
- Thinking mode compatibility improvements
- Tool calling enhancements

## Decision Framework

Choose **Option 4** if:
- ✅ Users absolutely need both thinking and tools together
- ✅ Willing to accept 2-3x cost and latency
- ✅ Have development resources for complex implementation
- ✅ Can handle parsing edge cases and maintenance overhead

Choose **Option 2** if:
- ✅ Need reliable, simple solution
- ✅ Cost and latency are important
- ✅ Users can work with either thinking OR tools
- ✅ Prefer minimal maintenance overhead

## Conclusion

Option 4 provides full functionality but at significant complexity cost. Given the proxy service context where reliability and simplicity are often more valuable than feature completeness, Option 2 remains the more practical choice for most use cases.

However, this detailed specification preserves the Option 4 approach for future implementation if requirements change or if the cost/complexity trade-offs become more favorable.