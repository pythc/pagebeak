"""Select only runtime targets with recorded archive checksums."""
import json
import platform

from akb.config import ROOT


def runtime_target(system=None, machine=None):
    key = (system or platform.system(), machine or platform.machine())
    targets = {("Darwin", "arm64"): "darwin-arm64", ("Linux", "x86_64"): "linux-x64"}
    if key not in targets:
        raise ValueError("腾讯服务运行时仅核准 macOS arm64 和 Linux x86_64；其他平台需单独验证")
    return targets[key]


def node_executable():
    manifest = json.loads((ROOT / "integrations/tencent-memory/upstream.json").read_text())
    return ROOT / "work/tencent-memory" / f"node-v{manifest['node_version']}-{runtime_target()}" / "bin/node"
