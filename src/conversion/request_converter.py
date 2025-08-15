import json
from typing import Dict, Any, List
from venv import logger
from src.core.constants import Constants
from src.models.claude import (
    ClaudeMessagesRequest,
    ClaudeMessage,
    ClaudeContentBlockThinking,
)
from src.core.config import config
import logging

logger = logging.getLogger(__name__)


def convert_claude_to_openai(
    claude_request: ClaudeMessagesRequest, model_manager
) -> Dict[str, Any]:
    """Convert Claude API request format to OpenAI format."""

    thinking_enabled = model_manager.should_enable_thinking(claude_request)

    # If thinking is enabled, patch any assistant messages that use tools but lack a thinking block.
    # Only do this if thinking was explicitly requested, not auto-enabled
    if thinking_enabled and claude_request.thinking and claude_request.thinking.enabled:
        injection_count = 0
        for msg in claude_request.messages:
            if msg.role == Constants.ROLE_ASSISTANT and isinstance(msg.content, list):
                has_tool_use = any(
                    getattr(b, "type", None) == Constants.CONTENT_TOOL_USE
                    for b in msg.content
                )
                has_thinking = any(
                    getattr(b, "type", None) == Constants.CONTENT_THINKING
                    for b in msg.content
                )

                # Per Anthropic docs, if an assistant message has tool use, it must also have a thinking block.
                if has_tool_use and not has_thinking:
                    logger.info(
                        "Injecting placeholder thinking block into an assistant message for compatibility."
                    )
                    msg.content.insert(
                        0, ClaudeContentBlockThinking(type="thinking", thinking="...")
                    )
                    injection_count += 1
        
        if injection_count > 0:
            logger.info(f"Injected {injection_count} thinking blocks for thinking-enabled conversation")
    elif thinking_enabled:
        logger.debug("Thinking enabled but no explicit thinking request - skipping injection")

    # Map model
    openai_model = model_manager.map_claude_model_to_openai(claude_request.model)

    # Convert messages
    openai_messages = []

    # Add system message if present
    if claude_request.system:
        system_text = ""
        if isinstance(claude_request.system, str):
            system_text = claude_request.system
        elif isinstance(claude_request.system, list):
            text_parts = []
            for block in claude_request.system:
                if hasattr(block, "type") and block.type == Constants.CONTENT_TEXT:
                    text_parts.append(block.text)
                elif (
                    isinstance(block, dict)
                    and block.get("type") == Constants.CONTENT_TEXT
                ):
                    text_parts.append(block.get("text", ""))
            system_text = "\n\n".join(text_parts)

        if system_text.strip():
            openai_messages.append(
                {"role": Constants.ROLE_SYSTEM, "content": system_text.strip()}
            )

    # Process Claude messages
    i = 0
    while i < len(claude_request.messages):
        msg = claude_request.messages[i]

        if msg.role == Constants.ROLE_USER:
            openai_message = convert_claude_user_message(msg)
            openai_messages.append(openai_message)
        elif msg.role == Constants.ROLE_ASSISTANT:
            openai_message = convert_claude_assistant_message(msg, thinking_enabled)
            openai_messages.append(openai_message)

            # Check if next message contains tool results
            if i + 1 < len(claude_request.messages):
                next_msg = claude_request.messages[i + 1]
                if (
                    next_msg.role == Constants.ROLE_USER
                    and isinstance(next_msg.content, list)
                    and any(
                        block.type == Constants.CONTENT_TOOL_RESULT
                        for block in next_msg.content
                        if hasattr(block, "type")
                    )
                ):
                    # Process tool results
                    i += 1  # Skip to tool result message
                    # Use standard OpenAI tool message format for all cases
                    # This maintains compatibility with all OpenAI-compatible endpoints
                    tool_results = convert_claude_tool_results(next_msg)
                    openai_messages.extend(tool_results)

        i += 1

    # Build OpenAI request
    openai_request = {
        "model": openai_model,
        "messages": openai_messages,
        "max_tokens": min(
            max(claude_request.max_tokens, config.min_tokens_limit),
            config.max_tokens_limit,
        ),
        "temperature": claude_request.temperature,
        "stream": claude_request.stream,
    }
    logger.debug(
        f"Converted Claude request to OpenAI format: {json.dumps(openai_request, indent=2, ensure_ascii=False)}"
    )
    # Add optional parameters
    if claude_request.stop_sequences:
        openai_request["stop"] = claude_request.stop_sequences
    if claude_request.top_p is not None:
        openai_request["top_p"] = claude_request.top_p

    # Convert tools
    if claude_request.tools:
        openai_tools = []
        for tool in claude_request.tools:
            if tool.name and tool.name.strip():
                openai_tools.append(
                    {
                        "type": Constants.TOOL_FUNCTION,
                        Constants.TOOL_FUNCTION: {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.input_schema,
                        },
                    }
                )
        if openai_tools:
            openai_request["tools"] = openai_tools

    # Convert tool choice
    if claude_request.tool_choice:
        choice_type = claude_request.tool_choice.get("type")
        if choice_type == "auto":
            openai_request["tool_choice"] = "auto"
        elif choice_type == "any":
            openai_request["tool_choice"] = "auto"
        elif choice_type == "tool" and "name" in claude_request.tool_choice:
            openai_request["tool_choice"] = {
                "type": Constants.TOOL_FUNCTION,
                Constants.TOOL_FUNCTION: {"name": claude_request.tool_choice["name"]},
            }
        else:
            openai_request["tool_choice"] = "auto"

    # Add thinking if enabled, using extra_body to pass non-standard parameters
    if model_manager.should_enable_thinking(claude_request):
        logger.info("Thinking is enabled for this request.")
        thinking_params = model_manager.get_thinking_params(claude_request.model)
        if "extra_body" not in openai_request:
            openai_request["extra_body"] = {}
        openai_request["extra_body"].update(thinking_params)

    # Validate tool use/result pairing to prevent API errors
    if claude_request.tools:
        is_valid = validate_tool_use_pairing(openai_messages, thinking_enabled)
        if not is_valid:
            logger.error("Tool use/result validation failed - this may cause API errors")
            # Consider raising an exception here in strict mode
            
    return openai_request


def convert_claude_user_message(msg: ClaudeMessage) -> Dict[str, Any]:
    """Convert Claude user message to OpenAI format."""
    if msg.content is None:
        return {"role": Constants.ROLE_USER, "content": ""}
    
    if isinstance(msg.content, str):
        return {"role": Constants.ROLE_USER, "content": msg.content}

    # Handle multimodal content
    openai_content = []
    for block in msg.content:
        if block.type == Constants.CONTENT_TEXT:
            openai_content.append({"type": "text", "text": block.text})
        elif block.type == Constants.CONTENT_IMAGE:
            # Convert Claude image format to OpenAI format
            if (
                isinstance(block.source, dict)
                and block.source.get("type") == "base64"
                and "media_type" in block.source
                and "data" in block.source
            ):
                openai_content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{block.source['media_type']};base64,{block.source['data']}"
                        },
                    }
                )

    if len(openai_content) == 1 and openai_content[0]["type"] == "text":
        return {"role": Constants.ROLE_USER, "content": openai_content[0]["text"]}
    else:
        return {"role": Constants.ROLE_USER, "content": openai_content}


def convert_claude_assistant_message(msg: ClaudeMessage, thinking_enabled: bool = False) -> Dict[str, Any]:
    """Convert Claude assistant message to OpenAI format, ensuring thinking blocks are first when thinking is enabled."""
    text_blocks = []
    tool_calls = []
    tool_use_blocks = []
    thinking_blocks = []

    if msg.content is None:
        return {"role": Constants.ROLE_ASSISTANT, "content": None}
    
    if isinstance(msg.content, str):
        # If thinking is enabled and we have a simple string, we need to add a thinking block
        if thinking_enabled:
            return {
                "role": Constants.ROLE_ASSISTANT, 
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "text", "text": msg.content}
                ]
            }
        return {"role": Constants.ROLE_ASSISTANT, "content": msg.content}

    for block in msg.content:
        if block.type == Constants.CONTENT_TEXT:
            text_blocks.append({"type": "text", "text": block.text})
        elif block.type == Constants.CONTENT_TOOL_USE:
            # When thinking is enabled, tool uses go in content blocks
            # When thinking is disabled, they go in separate tool_calls field
            if thinking_enabled:
                tool_use_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input
                })
            else:
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": Constants.TOOL_FUNCTION,
                        Constants.TOOL_FUNCTION: {
                            "name": block.name,
                            "arguments": json.dumps(block.input, ensure_ascii=False),
                        },
                    }
                )
        elif block.type in [Constants.CONTENT_THINKING, Constants.CONTENT_REDACTED_THINKING]:
            thinking_blocks.append(block.model_dump(exclude_none=True))

    # When thinking is enabled, ensure there's at least one thinking block at the start
    if thinking_enabled and tool_use_blocks and not thinking_blocks:
        thinking_blocks.append({"type": "thinking", "thinking": "..."})

    # Handle content ordering - always preserve original structure when possible
    if thinking_enabled and tool_use_blocks:
        # When thinking is enabled with tools, we need to be careful about ordering
        # Only merge text into thinking if there's no existing thinking content
        if text_blocks and thinking_blocks and thinking_blocks[0].get("thinking") == "...":
            # Only merge if the thinking block is a placeholder
            text_content = " ".join([block["text"] for block in text_blocks if block.get("text")])
            if text_content.strip():
                thinking_blocks[0]["thinking"] = text_content
                # Remove text blocks since they're now in thinking
                text_blocks = []
                logger.debug("Merged placeholder thinking with text content")
        
        # Preserve proper ordering: thinking -> text -> tools (if text wasn't merged)
        openai_content = thinking_blocks + text_blocks + tool_use_blocks
        logger.debug(f"Thinking enabled with tools: {len(thinking_blocks)} thinking + {len(text_blocks)} text + {len(tool_use_blocks)} tool blocks")
    else:
        # Normal ordering: thinking blocks first, then text, then tool_use blocks
        openai_content = thinking_blocks + text_blocks + tool_use_blocks

    openai_message = {"role": Constants.ROLE_ASSISTANT}

    # Set content field based on thinking enablement
    if thinking_enabled:
        # When thinking is enabled, always use content blocks format
        openai_message["content"] = openai_content if openai_content else None
    elif not openai_content and not tool_calls:
        openai_message["content"] = None
    elif len(openai_content) == 1 and openai_content[0]["type"] == "text" and not tool_calls:
        # Simplify to a string if it's just a single text block and no tool calls
        openai_message["content"] = openai_content[0]["text"]
    elif openai_content:
        # Pass as a list of content blocks
        openai_message["content"] = openai_content
    else:
        openai_message["content"] = None

    # Set tool calls only when thinking is disabled
    if tool_calls and not thinking_enabled:
        openai_message["tool_calls"] = tool_calls

    return openai_message


def convert_claude_tool_results(msg: ClaudeMessage) -> List[Dict[str, Any]]:
    """Convert Claude tool results to OpenAI format."""
    tool_messages = []

    if isinstance(msg.content, list):
        for block in msg.content:
            if block.type == Constants.CONTENT_TOOL_RESULT:
                content = parse_tool_result_content(block.content)
                tool_messages.append(
                    {
                        "role": Constants.ROLE_TOOL,
                        "tool_call_id": block.tool_use_id,
                        "content": content,
                    }
                )

    return tool_messages



def parse_tool_result_content(content):
    """Parse and normalize tool result content into a string format."""
    if content is None:
        return "No content provided"

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        result_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == Constants.CONTENT_TEXT:
                result_parts.append(item.get("text", ""))
            elif isinstance(item, str):
                result_parts.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    result_parts.append(item.get("text", ""))
                else:
                    try:
                        result_parts.append(json.dumps(item, ensure_ascii=False))
                    except:
                        result_parts.append(str(item))
        return "\n".join(result_parts).strip()

    if isinstance(content, dict):
        if content.get("type") == Constants.CONTENT_TEXT:
            return content.get("text", "")
        try:
            return json.dumps(content, ensure_ascii=False)
        except:
            return str(content)

    try:
        return str(content)
    except:
        return "Unparseable content"


def validate_tool_use_pairing(openai_messages: List[Dict[str, Any]], thinking_enabled: bool) -> bool:
    """Validate that tool use and tool result messages are properly paired."""
    pending_tool_calls = set()
    
    for i, message in enumerate(openai_messages):
        role = message.get("role")
        
        if role == Constants.ROLE_ASSISTANT:
            # Extract tool call IDs from different formats
            if thinking_enabled and isinstance(message.get("content"), list):
                # Thinking-enabled format: tool calls in content blocks
                for block in message["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id = block.get("id")
                        if tool_id:
                            pending_tool_calls.add(tool_id)
            else:
                # Traditional format: tool calls in separate field
                tool_calls = message.get("tool_calls", [])
                for tool_call in tool_calls:
                    tool_id = tool_call.get("id")
                    if tool_id:
                        pending_tool_calls.add(tool_id)
        
        elif role == Constants.ROLE_TOOL:
            # Traditional tool result format
            tool_call_id = message.get("tool_call_id")
            if tool_call_id in pending_tool_calls:
                pending_tool_calls.remove(tool_call_id)
            else:
                logger.warning(f"Tool result references non-existent tool call: {tool_call_id}")
                return False
        
        elif role == Constants.ROLE_USER and thinking_enabled and isinstance(message.get("content"), list):
            # Thinking-enabled format: tool results in user message content
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id in pending_tool_calls:
                        pending_tool_calls.remove(tool_use_id)
                    else:
                        logger.warning(f"Tool result references non-existent tool use: {tool_use_id}")
                        return False
    
    if pending_tool_calls:
        logger.warning(f"Unmatched tool calls remain: {pending_tool_calls}")
        return False
    
    return True
