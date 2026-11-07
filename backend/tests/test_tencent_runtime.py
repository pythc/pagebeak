import json

import pytest
from akb.config import ROOT
from akb.tencent_runtime import runtime_target


def test_only_checksum_pinned_platforms_can_start():
    manifest = json.loads((ROOT / "integrations/tencent-memory/upstream.json").read_text())
    for system, machine in [("Darwin", "arm64"), ("Linux", "x86_64")]:
        target = runtime_target(system, machine)
        assert len(manifest["node_" + target.replace("-", "_") + "_sha256"]) == 64
    assert len(manifest["sqlite_linux_x64_prebuild_sha256"]) == 64
    for system, machine in [("Linux", "aarch64"), ("Windows", "AMD64"), ("Darwin", "x86_64")]:
        with pytest.raises(ValueError):
            runtime_target(system, machine)
