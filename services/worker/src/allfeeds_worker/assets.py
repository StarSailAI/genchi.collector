from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from allfeeds_contracts import ResourceAsset
from allfeeds_sdk import AssetStorePlugin, ConfigurationError


def _segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned[:100] or hashlib.sha256(value.encode()).hexdigest()[:20]


class LocalAssetStore(AssetStorePlugin):
    name = "local"

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("ASSET_STORE_PATH", "/data/assets"))

    def store(self, source_id: str, asset: ResourceAsset, content: bytes) -> ResourceAsset:
        target = (
            self.root
            / _segment(source_id)
            / _segment(asset.external_id)
            / _segment(asset.asset_key)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return asset.model_copy(
            update={
                "storage_uri": target.resolve().as_uri(),
                "size_bytes": len(content),
                "checksum": hashlib.sha256(content).hexdigest(),
            }
        )


class S3AssetStore(AssetStorePlugin):
    name = "s3"

    def __init__(self):
        self.bucket = os.environ.get("S3_BUCKET", "").strip()
        self.client = None

    def store(self, source_id: str, asset: ResourceAsset, content: bytes) -> ResourceAsset:
        if not self.bucket:
            raise ConfigurationError("S3_BUCKET is required for the s3 asset store")
        if self.client is None:
            import boto3

            self.client = boto3.client(
                "s3",
                endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
                region_name=os.environ.get("S3_REGION") or None,
            )
        prefix = os.environ.get("S3_PREFIX", "allfeeds").strip("/")
        key = "/".join(
            (prefix, _segment(source_id), _segment(asset.external_id), _segment(asset.asset_key))
        )
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=asset.media_type or "application/octet-stream",
        )
        return asset.model_copy(
            update={
                "storage_uri": f"s3://{self.bucket}/{key}",
                "size_bytes": len(content),
                "checksum": hashlib.sha256(content).hexdigest(),
            }
        )
