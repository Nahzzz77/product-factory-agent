"""Package-owned handbook resource discovery shared by every entry point."""

from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from typing import Iterator

from product_factory.errors import ErrorCategory, FactoryError


@contextmanager
def factory_resource_root() -> Iterator[Path]:
    """Materialize bundled handbooks without depending on the caller's cwd."""
    resource_root = files("product_factory").joinpath("resources")
    if resource_root.is_dir():
        with as_file(resource_root) as root:
            yield root
        return
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "references/handbooks/manifest.yaml").is_file():
        yield source_root
        return
    raise FactoryError(
        "handbook_invalid",
        ErrorCategory.INPUT_REQUIRED,
        "技术手册资源不可读取",
        "init",
        False,
        "重新安装包含技术手册的 product-factory 包",
    )
