"""Exercise installed entry points through real MCP transports, without credentials."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from youtube_mcp.__main__ import loopback_address


def clean_environment(tmp_path):
    # No inherited Google, cloud, FastMCP auth, proxy, or credential settings.
    return {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}


async def rpc(process, method, request_id, params=None):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    process.stdin.write((json.dumps(request) + "\n").encode())
    await process.stdin.drain()
    # Every stdout line must be protocol JSON, including during startup.
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
        assert line, "server exited without a response"
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"
        if message.get("id") == request_id:
            assert "error" not in message
            return message["result"]


async def test_same_schema_and_health_over_both_transports(tmp_path):
    env = clean_environment(tmp_path)
    # Fail startup/tool calls if any dependency attempts an external connection.
    guard = tmp_path / "sitecustomize.py"
    guard.write_text(
        "import ipaddress, socket\n"
        "original = socket.socket.connect\n"
        "def connect(self, address):\n"
        "    if self.family in (socket.AF_INET, socket.AF_INET6):\n"
        "        assert ipaddress.ip_address(address[0]).is_loopback, address\n"
        "    return original(self, address)\n"
        "socket.socket.connect = connect\n"
    )
    env["PYTHONPATH"] = str(tmp_path)
    with (tmp_path / "stdio.log").open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "youtube_mcp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            env=env,
        )
        try:
            await rpc(
                process,
                "initialize",
                1,
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "slice1-test", "version": "1"},
                },
            )
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await process.stdin.drain()
            stdio_tools = (await rpc(process, "tools/list", 2))["tools"]
            stdio_health = await rpc(
                process,
                "tools/call",
                3,
                {
                    "name": "health",
                    "arguments": {},
                },
            )
            assert not stdio_health.get("isError", False)
            assert json.loads(stdio_health["content"][0]["text"]) == {"status": "ok"}
        finally:
            process.stdin.close()
            await asyncio.wait_for(process.wait(), timeout=10)
        for line in (await process.stdout.read()).splitlines():
            assert json.loads(line)["jsonrpc"] == "2.0"
        assert process.returncode == 0
    assert "Starting MCP server" in (tmp_path / "stdio.log").read_text()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (tmp_path / "http.log").open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "youtube_mcp",
            "--transport",
            "http",
            "--port",
            str(port),
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            env=env,
        )
        try:
            # Wait on the listener rather than guessing a startup delay.
            async with asyncio.timeout(20):
                while True:
                    assert process.returncode is None, (tmp_path / "http.log").read_text()
                    try:
                        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(0.05)
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                http_tools = [
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for tool in await client.list_tools()
                ]
                assert http_tools == stdio_tools
                assert [tool["name"] for tool in http_tools] == [
                    "health",
                    "get_channel",
                    "get_video",
                    "list_playlists",
                    "list_playlist_items",
                    "upload_video",
                    "resume_upload",
                    "set_thumbnail",
                ]
                result = await client.call_tool("health", {})
                assert result.data == {"status": "ok"}
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=10)
        assert await process.stdout.read() == b""


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.0.2.1", "localhost", "example.com", "::ffff:127.0.0.1"]
)
def test_unauthenticated_remote_bind_exits_before_startup(host, tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "youtube_mcp", "--transport", "http", "--host", host],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=clean_environment(tmp_path),
    )
    assert result.returncode == 2
    assert "error:" in result.stderr
    assert "Starting MCP server" not in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "::1"])
def test_literal_loopback_addresses(host):
    assert loopback_address(host) == host


def test_no_google_dependency():
    from importlib.metadata import requires

    assert not any("google" in item.lower() for item in requires("youtube-mcp"))
    assert not any(name == "google" or name.startswith("google.") for name in sys.modules)


def test_console_script_installed(tmp_path):
    executable = Path(sys.executable).with_name("youtube-mcp")
    result = subprocess.run(
        [str(executable), "--help"],
        check=False,
        capture_output=True,
        timeout=10,
        env=clean_environment(tmp_path),
    )
    assert result.returncode == 0
    assert b"--transport" in result.stdout
