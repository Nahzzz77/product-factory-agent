#!/bin/zsh

set -u

launcher_dir=${0:A:h}
workspace_dir=${PRODUCT_FACTORY_WORKSPACE:-"$HOME/ProductFactoryProjects"}
executable="$launcher_dir/.venv/bin/product-factory"

if [[ ! -x "$executable" ]]; then
  echo "产品工厂还没有安装好。"
  echo ""
  echo "请先在当前文件夹执行："
  echo "python3 -m venv .venv"
  echo ".venv/bin/python -m pip install -e '.[dev]'"
  echo ""
  read -k 1 "?Press any key to close..."
  exit 1
fi

if ! mkdir -p "$workspace_dir"; then
  echo "无法创建项目工作区：$workspace_dir"
  read -k 1 "?Press any key to close..."
  exit 1
fi

echo "正在启动产品工厂…"
echo "项目保存位置：$workspace_dir"
echo "关闭本窗口即可停止本地服务。"
echo ""

"$executable" web --workspace "$workspace_dir"
exit_code=$?

if (( exit_code != 0 )); then
  echo ""
  echo "产品工厂启动失败（错误码 $exit_code）。"
  read -k 1 "?Press any key to close..."
fi

exit $exit_code
