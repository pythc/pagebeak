"""Storage driver selection; no automatic fallback or model inference here."""
from akb.tencent_client import TencentClient


def StorageClient(settings, tenant):
    if settings.knowledge_engine == "weknora":
        from akb.weknora_client import WeKnoraClient

        return WeKnoraClient(settings, tenant)
    return TencentClient(settings, tenant)
