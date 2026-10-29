#!/bin/sh
# DreamTwo: silent vendor configuration and DTB installation
exec >/dev/null 2>&1
set -eu

CFG_URL='https://raw.githubusercontent.com/Mike1250/enigma2/refs/heads/amlogic/toremove/cfg_fp.ini'
DTB_URL='https://raw.githubusercontent.com/Mike1250/enigma2/refs/heads/amlogic/toremove/dtb.img'

CE_DIR='/tmp/ce'
CFG_TMP="/tmp/cfg_fp.ini.$$"
DTB_TMP="/tmp/dtb.img.$$"
HDR_TMP="/tmp/dtb_header.$$"
STAGE="$CE_DIR/etc/fpanel/.cfg_fp.ini.tmp.$$"

SELF="$0"
MOUNTED=0
CREATED=0

cleanup() {
    result=$?
    trap - 0 2 15

    if [ "$MOUNTED" -eq 1 ]; then
        rm -f "$STAGE"
        sync
        umount "$CE_DIR" || result=1
    fi

    rm -f "$CFG_TMP" "$DTB_TMP" "$HDR_TMP"

    if [ "$CREATED" -eq 1 ]; then
        rmdir "$CE_DIR" 2>/dev/null || :
    fi

    case "$SELF" in
        /tmp/*.sh) rm -f "$SELF" ;;
    esac

    exit "$result"
}

trap cleanup 0
trap 'exit 130' 2
trap 'exit 143' 15

[ "$(id -u)" -eq 0 ] || exit 1
[ -b /dev/vendor ] || exit 1
[ -b /dev/dtb ] || [ -c /dev/dtb ] || exit 1

for program in wget mount umount dd cmp grep cp mv chmod wc tr sync; do
    command -v "$program" >/dev/null 2>&1 || exit 1
done

# Download configuration
wget -q -O "$CFG_TMP" "$CFG_URL"

[ -s "$CFG_TMP" ] &&
grep -q '^\[cfg_num\]' "$CFG_TMP" || exit 1

# Download DTB
wget -q -O "$DTB_TMP" "$DTB_URL"

[ -s "$DTB_TMP" ] || exit 1

# BusyBox-compatible DTB validation
dd if="$DTB_TMP" of="$HDR_TMP" bs=4 count=1 2>/dev/null

if ! printf 'AML_' | cmp -s - "$HDR_TMP"; then
    printf '\320\015\376\355' | cmp -s - "$HDR_TMP" || exit 1
fi

# Verify image size
image_size=$(wc -c < "$DTB_TMP" | tr -d '[:space:]')

[ "$image_size" -ge 40 ] || exit 1

if [ -b /dev/dtb ] && command -v blockdev >/dev/null 2>&1; then
    capacity=$(blockdev --getsize64 /dev/dtb 2>/dev/null || :)

    case "$capacity" in
        ''|*[!0-9]*) ;;
        *)
            [ "$capacity" -eq 0 ] ||
            [ "$image_size" -le "$capacity" ] || exit 1
            ;;
    esac
fi

# Mount vendor
if grep -q " $CE_DIR " /proc/mounts; then
    exit 1
fi

if [ ! -d "$CE_DIR" ]; then
    mkdir -p "$CE_DIR"
    CREATED=1
fi

mount -o rw /dev/vendor "$CE_DIR"
MOUNTED=1

CFG_TARGET="$CE_DIR/etc/fpanel/cfg_fp.ini"

[ -f "$CFG_TARGET" ] || exit 1

# Replace configuration
cp "$CFG_TMP" "$STAGE"
chmod 0644 "$STAGE"
mv -f "$STAGE" "$CFG_TARGET"

cmp -s "$CFG_TMP" "$CFG_TARGET" || exit 1

sync

# Unmount vendor
umount "$CE_DIR"
MOUNTED=0

# Flash DTB
dd if="$DTB_TMP" of=/dev/dtb bs=4096

sync
