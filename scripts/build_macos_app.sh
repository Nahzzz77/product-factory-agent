#!/bin/zsh

set -euo pipefail

repository_dir=${0:A:h:h}
bundle_dir="$repository_dir/产品工厂.app"
resources_dir="$bundle_dir/Contents/Resources"
runtime_source="$repository_dir/.venv"
application_source="$repository_dir/src/product_factory"

if [[ ! -x "$runtime_source/bin/python" ]]; then
  echo "缺少 .venv，请先完成本地安装。" >&2
  exit 1
fi

/bin/mkdir -p "$resources_dir/runtime" "$resources_dir/application/product_factory"
/usr/bin/ditto "$runtime_source" "$resources_dir/runtime"
/usr/bin/ditto "$application_source" "$resources_dir/application/product_factory"
/bin/chmod +x "$bundle_dir/Contents/MacOS/产品工厂"
/usr/bin/codesign --force --deep --sign - "$bundle_dir"

echo "已生成：$bundle_dir"
