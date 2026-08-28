#!/bin/zsh

set -eu

script_dir=${0:A:h}
repo_dir=${script_dir:h}
cd "$repo_dir"

version=$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -n 1)
[[ -n "$version" ]] || { echo "无法读取版本号。" >&2; exit 1; }

python_executable=${PRODUCT_FACTORY_BUILD_PYTHON:-"$repo_dir/.venv/bin/python"}
[[ -x "$python_executable" ]] || python_executable=$(command -v python3)

dist_dir="$repo_dir/dist"
package_name="product-factory-agent-$version-macos"
temp_dir=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/product-factory-package.XXXXXX")
package_root="$temp_dir/$package_name"
app_root="$package_root/产品工厂.app"

cleanup() {
  /bin/rm -rf "$temp_dir"
}
trap cleanup EXIT

/bin/mkdir -p "$dist_dir" "$package_root/packages" "$app_root/Contents/MacOS"

echo "正在构建 Python 程序包……"
"$python_executable" -m pip wheel . --no-deps --no-build-isolation --wheel-dir "$package_root/packages"

wheel_files=("$package_root"/packages/product_factory_agent-*.whl(N))
(( ${#wheel_files[@]} == 1 )) || { echo "wheel 构建结果异常。" >&2; exit 1; }
/bin/cp "${wheel_files[1]}" "$dist_dir/"

echo "正在组装 macOS 安装包……"
/bin/cp "产品工厂.app/Contents/Info.plist" "$app_root/Contents/Info.plist"
/bin/cp "产品工厂.app/Contents/PkgInfo" "$app_root/Contents/PkgInfo"
/bin/cp "产品工厂.app/Contents/MacOS/产品工厂" "$app_root/Contents/MacOS/产品工厂"
/bin/chmod +x "$app_root/Contents/MacOS/产品工厂"
/bin/cp scripts/install_macos.command "$package_root/安装产品工厂.command"
/bin/chmod +x "$package_root/安装产品工厂.command"
/bin/cp RELEASE-README.txt "$package_root/使用说明.txt"
/bin/cp LICENSE "$package_root/LICENSE"

echo "正在检查隐私边界……"
if find "$package_root" \( \
  -name '.product-factory' -o \
  -name 'agent-runs' -o \
  -name 'events.jsonl' -o \
  -name 'approvals.jsonl' -o \
  -name 'output.log' \
\) -print -quit | /usr/bin/grep -q .; then
  echo "安装包中发现用户项目或运行记录，已经停止。" >&2
  exit 1
fi
local_home=${HOME:-}
if [[ -n "$local_home" ]] && /usr/bin/grep -R -F -l "$local_home" "$package_root" >/dev/null; then
  echo "安装包中发现本机绝对路径，已经停止。" >&2
  exit 1
fi

archive="$dist_dir/$package_name.zip"
/bin/rm -f "$archive"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$package_root" "$archive"

(
  cd "$dist_dir"
  /usr/bin/shasum -a 256 "${archive:t}" "${wheel_files[1]:t}" > SHA256SUMS.txt
)

echo "发布文件已生成："
echo "$archive"
echo "$dist_dir/${wheel_files[1]:t}"
echo "$dist_dir/SHA256SUMS.txt"
