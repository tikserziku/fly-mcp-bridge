"""
MCP SSE Bridge on Fly.io
Proxies MCP SSE/message to Oracle VM and Kimi VM
Claude Web connects here -> Fly forwards to VMs
"""
import os
import json
import uuid
import time
import threading
import requests
from flask import Flask, Response, request, jsonify, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# Backend VMs
ORACLE_VM = os.getenv("ORACLE_VM_URL", "https://mcp.92-5-72-169.sslip.io")
KIMI_VM = os.getenv("KIMI_VM_URL", "https://kimi-vm.34-72-175-66.sslip.io")
KIMI_MCP_PATH = "/mcp/5d39aa90c50dfeda2f875f38bff906c1"
KIMI_API_KEY = os.getenv("KIMI_MCP_KEY", "KimiVM_Secure_Key_2026")
WINDOWS_TASK_URL = os.getenv("WINDOWS_TASK_URL", "https://92-5-72-169.sslip.io")
API_KEY = os.getenv("MCP_API_KEY", "fly-mcp-bridge-2026")

sessions = {}

ORACLE_TOOLS = [
    {"name": "oracle_run_command", "description": "Execute bash command on Oracle VM (92.5.72.169). Linux with 33+ services.", "inputSchema": {"type": "object", "properties": {"command": {"type": "string", "description": "Bash command"}, "timeout": {"type": "number"}}, "required": ["command"]}},
    {"name": "oracle_read_file", "description": "Read file from Oracle VM", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "oracle_write_file", "description": "Write file to Oracle VM", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "oracle_list_files", "description": "List directory on Oracle VM", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "oracle_service_status", "description": "Check systemd service status on Oracle VM", "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]}},
    {"name": "oracle_service_logs", "description": "Get service logs from Oracle VM", "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}, "lines": {"type": "number"}}, "required": ["service"]}},
    {"name": "oracle_restart_service", "description": "Restart service on Oracle VM", "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]}},
    {"name": "oracle_list_services", "description": "List all grok-* services on Oracle VM", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "oracle_system_status", "description": "Oracle VM system status (CPU, RAM, disk)", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "oracle_send_telegram", "description": "Send Telegram message via Oracle VM", "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
]

KIMI_TOOLS = [
    {"name": "kimi_run_command", "description": "Execute bash command on Kimi VM (34.72.175.66). GCP e2-micro, Debian.", "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]}},
    {"name": "kimi_read_file", "description": "Read file from Kimi VM", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "kimi_write_file", "description": "Write file to Kimi VM", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "kimi_ask_kimi", "description": "Ask Kimi K2.5 AI model a question", "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
    {"name": "kimi_system_status", "description": "Kimi VM system status", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "kimi_manage_service", "description": "Manage services on Kimi VM (status/restart/stop/start/logs)", "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}, "action": {"type": "string"}}, "required": ["service", "action"]}},
]

WINDOWS_TOOLS = [
    {"name": "windows_execute", "description": "Send task to Windows PC. Picked up by auto_agent.py.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "description": "Task description"}, "priority": {"type": "string"}}, "required": ["task"]}},
    {"name": "windows_status", "description": "Check Windows agent status", "inputSchema": {"type": "object", "properties": {}}},
]

ALL_TOOLS = ORACLE_TOOLS + KIMI_TOOLS + WINDOWS_TOOLS


def proxy_to_vm(vm_url, tool_name, args):
    if tool_name.startswith("oracle_"):
        backend_tool = tool_name[7:]
        url = ORACLE_VM
        endpoint = f"{url}/message?sessionId=bridge"
        headers = {"Content-Type": "application/json"}
    elif tool_name.startswith("kimi_"):
        backend_tool = tool_name[5:]
        url = KIMI_VM
        endpoint = f"{url}{KIMI_MCP_PATH}"
        headers = {"Content-Type": "application/json", "X-API-Key": KIMI_API_KEY}
    elif tool_name.startswith("windows_"):
        backend_tool = tool_name
        url = ORACLE_VM
        endpoint = f"{url}/message?sessionId=bridge"
        headers = {"Content-Type": "application/json"}
    else:
        return [{"type": "text", "text": f"Unknown tool prefix: {tool_name}"}]

    try:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": backend_tool, "arguments": args}
        }
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=120)
        if resp.status_code == 200:
            data = resp.json()
            result = data.get("result", {})
            return result.get("content", [{"type": "text", "text": json.dumps(result)}])
        elif resp.status_code == 202:
            data = resp.json()
            return [{"type": "text", "text": json.dumps(data)}]
        else:
            return [{"type": "text", "text": f"Backend error: {resp.status_code} {resp.text[:500]}"}]
    except Exception as e:
        return [{"type": "text", "text": f"Connection error to {url}: {str(e)[:300]}"}]


def handle_jsonrpc(body):
    method = body.get("method", "")
    rid = body.get("id")
    params = body.get("params", {})

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "fly-mcp-bridge", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        }}
    elif method == "notifications/initialized":
        return None
    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": ALL_TOOLS}}
    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        content = proxy_to_vm(None, name, args)
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": content}}
    elif method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


@app.route("/sse")
def sse():
    session_id = str(uuid.uuid4())
    sessions[session_id] = {"created": time.time()}

    def event_stream():
        msg_url = f"/message?sessionId={session_id}"
        yield f"event: endpoint\ndata: {msg_url}\n\n"
        while True:
            time.sleep(15)
            yield ": ping\n\n"

    resp = Response(stream_with_context(event_stream()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/message", methods=["POST"])
def message():
    session_id = request.args.get("sessionId", "")
    if session_id not in sessions and session_id != "bridge":
        return jsonify({"error": "Invalid session"}), 400

    body = request.json
    response = handle_jsonrpc(body)

    if response is None:
        return jsonify({"ok": True}), 202

    return jsonify(response)


@app.route("/")
def index():
    return jsonify({
        "name": "Fly MCP Bridge",
        "version": "1.0.0",
        "status": "ok",
        "tools": len(ALL_TOOLS),
        "backends": {"oracle_vm": ORACLE_VM, "kimi_vm": KIMI_VM},
        "endpoints": {"sse": "/sse", "message": "/message", "health": "/health"}
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "sessions": len(sessions)})


def cleanup():
    while True:
        time.sleep(300)
        now = time.time()
        expired = [k for k, v in sessions.items() if now - v["created"] > 3600]
        for k in expired:
            del sessions[k]

threading.Thread(target=cleanup, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
