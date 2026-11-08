#!/usr/bin/env bash
# Build/install the pinned Linux WFB driver without changing interfaces.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
kernel=$(uname -r)
kernel_source=${KERNEL_SOURCE:-/lib/modules/$kernel/build}
if [[ ! -f "$kernel_source/Makefile" ]]; then
 printf 'Matching kernel headers are missing: /lib/modules/%s/build\nInstall matching Linux headers or set KERNEL_SOURCE to a prepared matching tree.\n' "$kernel" >&2; exit 1
fi
sudo apt-get install -y --no-install-recommends build-essential git bc
source_dir="$root/vendor/rtl8812au-wfb"
commit=3577999972f350a2213e7509ae1f371b99fc96ab
mkdir -p "$root/vendor"
if [[ ! -d "$source_dir/.git" ]]; then
 if [[ -e "$source_dir" ]]; then printf 'Preserve the existing source directory before proceeding.\n' >&2; exit 1; fi
 git clone -b v5.2.20 --single-branch https://github.com/svpcom/rtl8812au.git "$source_dir"
fi
if [[ -n $(git -C "$source_dir" status --porcelain) ]]; then printf 'Driver source has local changes; stop.\n' >&2; exit 1; fi
git -C "$source_dir" checkout --detach "$commit"
case "$(uname -m)" in
 aarch64) platform=CONFIG_PLATFORM_ARM64_RPI; arch=arm64; pc_platform=n ;;
 armv7l) platform=CONFIG_PLATFORM_ARM_RPI; arch=arm; pc_platform=n ;;
 x86_64) platform=CONFIG_PLATFORM_I386_PC; arch=x86_64; pc_platform=y ;;
 *) printf 'Unsupported architecture.\n' >&2; exit 1 ;;
esac
actual_release=$(make -s -C "$kernel_source" kernelrelease)
if [[ "$actual_release" != "$kernel" ]]; then
 printf 'Kernel release mismatch: running %s, tree %s\n' "$kernel" "$actual_release" >&2; exit 1
fi
make -C "$source_dir" -j2 "KSRC=$kernel_source" "CONFIG_PLATFORM_I386_PC=$pc_platform" "$platform=y" "ARCH=$arch" USER_MODULE_NAME=88XXau
sudo install -D -m644 "$source_dir/88XXau_wfb.ko" "/lib/modules/$kernel/extra/88XXau_wfb.ko"
sudo depmod -a
printf 'Driver installed but not activated. Follow docs/WSL_RADIO.md using separate Ethernet/SSH access.\n'
