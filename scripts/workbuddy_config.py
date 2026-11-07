"""Write a local MCP template with absolute executable path and no real credential."""

import json
import sys

from akb.config import ROOT, Settings

target = ROOT / "work/workbuddy-mcp.json"
target.parent.mkdir(exist_ok=True)
config = {
    "mcpServers": {
        "personal-knowledge": {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "akb.mcp_server"],
            "env": {
                "AKB_API_URL": f"http://127.0.0.1:{Settings().api_port}",
                "AKB_TOKEN": "REPLACE_WITH_YOUR_PERSONAL_TOKEN",
                "AKB_EXPECTED_KB_ID": "REPLACE_WITH_KNOWLEDGE_BASE_ID_FROM_CONNECTION_PAGE",
            },
        }
    }
}
target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
print(f"Credential-free MCP configuration template: {target}")
