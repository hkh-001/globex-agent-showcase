"""商品向量身份：文本、服务、模型版本必须同时一致，才能复用。"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

from app.domain.catalog.product import Product
from app.infrastructure.settings import Settings


def normalized_embedding_endpoint(base_url: str) -> str:
    """Return the credential/query-free endpoint used by the vector identity."""
    url = urlsplit(base_url.strip())
    host = url.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    path = url.path.rstrip('/')
    return urlunsplit((url.scheme.lower(), host + (f':{url.port}' if url.port else ''), path, '', ''))


def embedding_identity(settings: Settings) -> str:
    endpoint = normalized_embedding_endpoint(settings.embedding_base_url)
    identity = {'endpoint': endpoint, 'model': settings.embedding_model,
                'version': settings.embedding_version, 'configured_dimension': settings.embedding_dim}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def product_embedding_payload(product: Product, model_key: str) -> dict[str, str]:
    text_hash = hashlib.sha256(product.searchable_text().encode()).hexdigest()
    return {'product_id': product.product_id, 'embedding_key': model_key,
            'text_hash': text_hash, 'embedding_schema': 'product-text-v1'}
