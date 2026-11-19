"""通用 Agent 适配器单测：工具清单、CLI 调用、安全门控、MCP 协议。"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_runtime import invoke, load_manifest  # noqa: E402


class TestAgentManifest(unittest.TestCase):
    def test_manifest_covers_all_registered_actions(self):
        manifest = load_manifest()
        modules = json.loads((ROOT / "config" / "modules.json").read_text(encoding="utf-8"))["modules"]
        tool_map = {t["name"]: t for t in manifest["tools"]}
        expected = {"logistics_list", "logistics_config", "logistics_doctor"}
        group_map = {"reimbursement": "reimb", "wage": "wage",
                     "summary": "summary", "monthly_sheet": "sheet"}
        for key, module in modules.items():
            for action in module["delegates"]:
                expected.add(f"logistics_{group_map[key]}_{action}")
        for action in ("parse", "plan", "run", "selftest"):
            expected.add(f"logistics_intake_{action}")

        self.assertTrue(expected.issubset(tool_map), sorted(expected - set(tool_map)))
        self.assertEqual(len(manifest["tools"]), len(tool_map))
        for tool in manifest["tools"]:
            self.assertIn(tool["risk"], {"read", "write", "external"})
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_regenerate_manifest_is_stable(self):
        current = load_manifest()
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "tool_manifest.py")],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        regenerated = load_manifest()
        self.assertEqual(current, regenerated)


class TestAgentCli(unittest.TestCase):
    def test_list_and_call(self):
        listed = subprocess.run(
            [sys.executable, str(SCRIPTS / "agent.py"), "list"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertGreaterEqual(len(json.loads(listed.stdout)["tools"]), 38)

        called = subprocess.run(
            [sys.executable, str(SCRIPTS / "agent.py"), "call", "logistics_list"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(called.returncode, 0, called.stderr)
        payload = json.loads(called.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["ok"])


class TestSafetyGate(unittest.TestCase):
    def test_write_tool_blocked_without_env(self):
        with mock.patch.dict(os.environ, {"LOGISTICS_ALLOW_WRITE": "", "LOGISTICS_ALLOW_EXTERNAL": ""}):
            result = invoke("logistics_summary_generate", {"args": ["--dry-run"]})
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["gate_env"], "LOGISTICS_ALLOW_WRITE")

    def test_help_is_always_allowed(self):
        with mock.patch.dict(os.environ, {"LOGISTICS_ALLOW_WRITE": "", "LOGISTICS_ALLOW_EXTERNAL": ""}):
            result = invoke("logistics_summary_generate", {"args": ["--help"]})
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("blocked"))


class TestMCPStdio(unittest.TestCase):
    def test_initialize_list_and_call(self):
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "agent.py"), "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(ROOT),
        )
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "selftest", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "logistics_doctor", "arguments": {}}},
        ]
        proc.stdin.write("\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)

        responses = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            msg = json.loads(line)
            responses[msg.get("id")] = msg

        self.assertIn("tools", responses[1]["result"]["capabilities"])
        self.assertGreaterEqual(len(responses[2]["result"]["tools"]), 38)
        call_result = responses[3]["result"]
        self.assertFalse(call_result["isError"])
        payload = json.loads(call_result["content"][0]["text"])
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
