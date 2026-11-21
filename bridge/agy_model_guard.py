"""Authorize paired bridge tool calls using Antigravity's native hook metadata."""

import json
import re
import sys
import time
import uuid

import bridge_server as bridge


def model_matches(expected, observed):
    if not isinstance(observed, str):
        return False
    if observed == expected:
        return True
    return bool(re.fullmatch(r"gemini-4-[a-z0-9]+", expected)) and any(
        observed == f"{expected}-{suffix}"
        for suffix in ("low", "medium", "high", "extra", "max", "tiered-low",
                       "tiered-medium", "tiered-high", "tiered-extra", "tiered-max")
    )


def _observe(item, state, observed):
    state.update(required_model=item["required_model"],
                 target_conversation_id=item["target_conversation_id"],
                 observed_model=observed, model_observed_at=bridge._now_iso())
    if model_matches(item["required_model"], observed):
        return ""
    code = "ANTIGRAVITY_MODEL_MISMATCH" if observed else "ANTIGRAVITY_MODEL_UNVERIFIED"
    state.update(status="blocked", error_code=code)
    state.pop("claim_proof", None)
    state.pop("reply_proof", None)
    return code


def _decode_argument(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def pre_tool(event):
    call = event.get("toolCall") or {}
    name = call.get("name", "")
    args = call.get("args") or {}
    wrapper = name == "call_mcp_tool"
    if wrapper:
        if _decode_argument(args.get("ServerName")) != "chatgpt-bridge":
            return {"decision": "allow"}
        name = _decode_argument(args.get("ToolName")) or ""
        original_arguments = args.get("Arguments")
        args = _decode_argument(original_arguments)
        args = _decode_argument(args)
        if not isinstance(args, dict):
            return {"decision": "allow"}
    if name.endswith("bridge_claim_codex_request"):
        purpose = "claim"
    elif name.endswith("bridge_reply_to_codex"):
        purpose = "reply"
    else:
        return {"decision": "allow"}
    request_id = args.get("request_id", "")
    if not isinstance(request_id, str):
        return {"decision": "deny", "reason": "INVALID_REQUEST_ID"}

    with bridge._mutex(bridge.BUS_DIR / "replies.mutex"):
        if purpose == "reply":
            request_id = request_id.strip()
            if not request_id:
                pending = bridge._pending_submissions()
                if len(pending) == 1:
                    request_id = pending[0]["request_id"]
        item = bridge._pending_request(request_id)
        if not item or not item.get("target_conversation_id"):
            return {"decision": "allow"}
        if event.get("conversationId") != item["target_conversation_id"]:
            return {"decision": "deny", "reason": "ANTIGRAVITY_TARGET_MISMATCH"}
        path = bridge._delivery_path(request_id)
        state = bridge._read_json_safe(path) or {}
        proof = uuid.uuid4().hex
        grant = {"value": proof, "expires_at": time.time() + 120}
        if item.get("required_model"):
            if purpose == "claim":
                if state.get("claimed_at"):
                    return {"decision": "deny", "reason": "ANTIGRAVITY_REQUEST_ALREADY_CLAIMED"}
                if bridge._active_guarded_request(item["target_conversation_id"]):
                    return {"decision": "deny", "reason": "ANTIGRAVITY_CONVERSATION_BUSY"}
            code = _observe(item, state, event.get("modelName"))
            if code:
                bridge._atomic_write_json(path, state)
                return {"decision": "deny", "reason": code}
            state.pop("error_code", None)
            state["status"] = "claimed" if state.get("claimed_at") else "model_verified"
            grant["model"] = event["modelName"]
        state[f"{purpose}_proof"] = grant
        bridge._atomic_write_json(path, state)
    changes = {"request_id": request_id, "model_proof": proof}
    if wrapper:
        args = {**args, **changes}
        encoded = json.dumps(args) if isinstance(original_arguments, str) else args
        if isinstance(_decode_argument(original_arguments), str):
            encoded = json.dumps(encoded)
        return {"decision": "allow", "overwrite": {"Arguments": encoded}}
    return {"decision": "allow", "overwrite": changes}


def pre_invocation(event):
    conversation_id = event.get("conversationId", "")
    if not isinstance(conversation_id, str) or not bridge.CONVERSATION_ID.fullmatch(conversation_id):
        return {}
    if not bridge._active_model_path(conversation_id).is_file():
        return {}
    with bridge._mutex(bridge.BUS_DIR / "replies.mutex"):
        item = bridge._active_guarded_request(conversation_id)
        if not item:
            return {}
        path = bridge._delivery_path(item["request_id"])
        state = bridge._read_json_safe(path) or {}
        was_blocked = state.get("status") == "blocked"
        code = _observe(item, state, event.get("modelName"))
        if not code:
            if was_blocked:
                state["status"] = "claimed"
                state.pop("error_code", None)
                bridge._atomic_write_json(path, state)
            return {}
        bridge._atomic_write_json(path, state)
    return {"terminationBehavior": "terminate", "injectSteps": [
        {"errorMessage": {"userMessage": f"{code}. Resume with the required model or cancel the blocked request from Codex."}},
    ]}


if __name__ == "__main__":
    handler = {"pre-tool": pre_tool, "pre-invocation": pre_invocation}[sys.argv[1]]
    print(json.dumps(handler(json.load(sys.stdin))))
