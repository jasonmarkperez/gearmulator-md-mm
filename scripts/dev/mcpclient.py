"""Minimal MCP client for the Gearmulator plugin server.

The embedded server answers JSON-RPC directly on ``POST /message``; the SSE
stream is only needed for server-initiated events, which no tool uses. That
keeps this client to plain stdlib HTTP.
"""

from __future__ import annotations

import json
import pathlib
import time
import urllib.error
import urllib.request

DISCOVERY_FILE = pathlib.Path.home() / ".gearmulator_mcp.json"


class McpError(RuntimeError):
    pass


def read_instances() -> list[dict]:
    if not DISCOVERY_FILE.is_file():
        return []
    try:
        data = json.loads(DISCOVERY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def find_instance(name_substring: str = "", pid: int | None = None) -> dict:
    """Resolve one live instance from the discovery file.

    Stale entries are common after a crash, so liveness is checked by pid
    rather than trusted from the file.
    """
    candidates = []
    for inst in read_instances():
        if pid is not None and inst.get("pid") != pid:
            continue
        if name_substring and name_substring.lower() not in str(inst.get("pluginName", "")).lower():
            continue
        if not _pid_alive(int(inst.get("pid", -1))):
            continue
        candidates.append(inst)

    if not candidates:
        raise McpError(
            f"no live MCP instance matching name={name_substring!r} pid={pid} "
            f"in {DISCOVERY_FILE}")
    if len(candidates) > 1:
        ports = [c.get("port") for c in candidates]
        raise McpError(f"ambiguous MCP instance match, ports={ports}; pass a pid")
    return candidates[0]


class McpClient:
    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 30.0):
        self.port = port
        self.host = host
        self.timeout = timeout
        self._id = 0

    @classmethod
    def connect(cls, name_substring: str = "", pid: int | None = None,
                **kwargs) -> "McpClient":
        inst = find_instance(name_substring, pid)
        return cls(int(inst["port"]), **kwargs)

    @classmethod
    def wait_for(cls, name_substring: str = "", pid: int | None = None,
                 timeout: float = 60.0, **kwargs) -> "McpClient":
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                client = cls.connect(name_substring, pid, **kwargs)
                client.health()
                return client
            except (McpError, OSError, urllib.error.URLError) as e:
                last = e
                time.sleep(0.25)
        raise McpError(f"MCP server did not come up within {timeout}s: {last}")

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params

        req = urllib.request.Request(
            f"http://{self.host}:{self.port}/message",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        if "error" in body:
            raise McpError(f"{method}: {body['error']}")
        return body.get("result", {})

    def health(self) -> dict:
        with urllib.request.urlopen(
                f"http://{self.host}:{self.port}/", timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def initialize(self, client_name: str = "gearmulator-dev") -> dict:
        return self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": client_name, "version": "1.0"},
            "capabilities": {},
        })

    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list").get("tools", [])

    def call(self, tool: str, **arguments):
        """Call a tool and return its parsed payload.

        MCP wraps results in a content array of text parts; tools here always
        emit one JSON text part, so unwrap it rather than making every caller do
        the same dance.
        """
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})

        if result.get("isError"):
            raise McpError(f"{tool}: {result}")

        content = result.get("content") or []
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if len(texts) != 1:
            return result
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
