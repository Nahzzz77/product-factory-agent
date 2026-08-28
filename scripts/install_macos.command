#!/bin/zsh

set -eu

package_dir=${0:A:h}
current_user=$(/usr/bin/id -un)
user_home=${PRODUCT_FACTORY_INSTALL_HOME:-${HOME:-"/Users/$current_user"}}
support_dir="$user_home/Library/Application Support/ProductFactory"
runtime_dir="$support_dir/runtime"
applications_dir="$user_home/Applications"
installed_app="$applications_dir/产品工厂.app"
source_app="$package_dir/产品工厂.app"
no_open=${PRODUCT_FACTORY_NO_OPEN:-0}

fail() {
  echo ""
  echo "安装失败：$1"
  echo ""
  echo "按回车键关闭窗口。"
  read -r
  exit 1
}

find_python() {
  local candidate
  local -a candidates

  if [[ -n "${PRODUCT_FACTORY_PYTHON:-}" ]]; then
    candidates=("$PRODUCT_FACTORY_PYTHON")
  else
    candidates=(python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3)
  fi

  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
      'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

echo "产品工厂安装程序"
echo "程序位置：$installed_app"
echo "用户项目：$user_home/ProductFactoryProjects"
echo ""

[[ -d "$source_app" ]] || fail "安装包中的应用文件缺失。请重新下载安装包。"

wheel_files=("$package_dir"/packages/product_factory_agent-*.whl(N))
(( ${#wheel_files[@]} == 1 )) || fail "安装包中的程序组件缺失或数量异常。"
wheel_file=${wheel_files[1]}

python_executable=$(find_python) || fail "需要 Python 3.11 或更高版本。请先安装 Python，再重新运行本程序。"

/bin/mkdir -p "$support_dir" "$applications_dir" || fail "无法创建程序目录。"
install_root=$(/usr/bin/mktemp -d "$support_dir/runtime-install.XXXXXX") || fail "无法创建临时安装目录。"
new_runtime="$install_root/runtime"

cleanup() {
  if [[ -d "$install_root" ]]; then
    /bin/rm -rf "$install_root"
  fi
}
trap cleanup EXIT

echo "正在创建独立运行环境……"
"$python_executable" -m venv "$new_runtime" || fail "无法创建 Python 运行环境。"

echo "正在安装程序组件……"
"$new_runtime/bin/python" -m pip install --disable-pip-version-check "$wheel_file" || \
  fail "程序组件安装失败。请检查网络连接后重试。"

timestamp=$(/bin/date '+%Y%m%d-%H%M%S')
if [[ -d "$runtime_dir" ]]; then
  /bin/mv "$runtime_dir" "$support_dir/runtime.previous-$timestamp" || fail "无法备份旧运行环境。"
fi
/bin/mv "$new_runtime" "$runtime_dir" || fail "无法启用新的运行环境。"

if [[ -d "$installed_app" ]]; then
  /bin/mv "$installed_app" "$applications_dir/产品工厂.previous-$timestamp.app" || fail "无法备份旧应用。"
fi
/usr/bin/ditto "$source_app" "$installed_app" || fail "无法复制应用。"
/usr/bin/codesign --force --deep --sign - "$installed_app" >/dev/null 2>&1 || fail "无法签署本地应用。"

echo ""
echo "安装完成。"
echo "程序：$installed_app"
echo "项目：$user_home/ProductFactoryProjects"
echo "项目目录不在应用或安装包中，升级程序不会覆盖项目。"

if [[ "$no_open" != "1" ]]; then
  /usr/bin/open "$installed_app"
fi

trap - EXIT
/bin/rm -rf "$install_root"

echo ""
echo "按回车键关闭窗口。"
read -r
