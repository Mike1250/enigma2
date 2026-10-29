#!/bin/sh
# DreamTwo / Amlogic: update /dev/vendor front-panel configuration and /dev/dtb.
# Run as root. This script removes its own /tmp copy when it exits.
set -eu

CFG_URL='https://raw.githubusercontent.com/Mike1250/enigma2/refs/heads/amlogic/toremove/cfg_fp.ini'
DTB_URL='https://github.com/Mike1250/enigma2/raw/refs/heads/amlogic/toremove/dtb.img'
CE_DIR='/tmp/ce'
CFG_TMP="/tmp/cfg_fp.ini.$$"
DTB_TMP="/tmp/dtb.img.$$"
STAGE="$CE_DIR/etc/fpanel/.cfg_fp.ini.tmp.$$"
SELF="$0"
MOUNTED=0
CREATED=0

cleanup() {
    status=$?
    trap - 0 2 15
    if [ "$MOUNTED" -eq 1 ]; then
        rm -f "$STAGE"
        sync
        umount "$CE_DIR" || echo 'WARNING: /dev/vendor could not be unmounted.' >&2
    fi
    rm -f "$CFG_TMP" "$DTB_TMP"
    if [ "$CREATED" -eq 1 ]; then
        rmdir "$CE_DIR" 2>/dev/null || :
    fi
    # Delete only the downloaded temporary script, not an arbitrary caller.
    case "$SELF" in
        /tmp/*.sh) rm -f "$SELF" ;;
    esac
    if [ "$status" -ne 0 ]; then
        echo "FAILED (exit $status). The DTB is flashed only after config installation succeeds." >&2
    fi
    exit "$status"
}
trap cleanup 0
trap 'exit 130' 2
trap 'exit 143' 15

[ "$(id -u)" -eq 0 ] || { echo 'Run this script as root.' >&2; exit 1; }
[ -b /dev/vendor ] || { echo '/dev/vendor is not a block device.' >&2; exit 1; }
[ -b /dev/dtb ] || [ -c /dev/dtb ] || {
    echo '/dev/dtb is not a device.' >&2
    exit 1
}
for program in wget mount umount dd od tr cmp; do
    command -v "$program" >/dev/null 2>&1 || {
        echo "Missing required command: $program" >&2
        exit 1
    }
done

printf '\n[1/5] Downloading front-panel configuration...\n'
wget -q -O "$CFG_TMP" "$CFG_URL"
[ -s "$CFG_TMP" ] && grep -q '^\[cfg_num\]' "$CFG_TMP" || {
    echo 'Downloaded cfg_fp.ini is missing or invalid.' >&2
    exit 1
}

printf '[2/5] Downloading DTB image...\n'
wget -q -O "$DTB_TMP" "$DTB_URL"
[ -s "$DTB_TMP" ] || { echo 'Downloaded dtb.img is empty.' >&2; exit 1; }
# Standard flattened DTB = d0 0d fe ed; Amlogic multi-DTB = AML_.
magic=$(dd if="$DTB_TMP" bs=4 count=1 2>/dev/null | od -An -tx1 | tr -d '[:space:]')
case "$magic" in
    d00dfeed|414d4c5f) ;;
    *) echo "Invalid DTB header ($magic), refusing to flash." >&2; exit 1 ;;
esac
# If the kernel exposes the DTB partition size, do not write beyond it.
if [ -b /dev/dtb ] && command -v blockdev >/dev/null 2>&1; then
    capacity=$(blockdev --getsize64 /dev/dtb 2>/dev/null || :)
    image_size=$(wc -c < "$DTB_TMP" | tr -d '[:space:]')
    case "$capacity" in
        ''|*[!0-9]*) ;;
        *)
            if [ "$capacity" -gt 0 ] && [ "$image_size" -gt "$capacity" ]; then
                echo "DTB image ($image_size bytes) exceeds device ($capacity bytes)." >&2
                exit 1
            fi
            ;;
    esac
fi

printf '[3/5] Mounting /dev/vendor at %s...\n' "$CE_DIR"
if grep -q " $CE_DIR " /proc/mounts; then
    echo "$CE_DIR is already mounted; refusing to interfere." >&2
    exit 1
fi
if [ ! -d "$CE_DIR" ]; then
    mkdir -p "$CE_DIR"
    CREATED=1
fi
mount -o rw /dev/vendor "$CE_DIR"
MOUNTED=1
CFG_TARGET="$CE_DIR/etc/fpanel/cfg_fp.ini"
[ -f "$CFG_TARGET" ] || {
    echo "$CFG_TARGET not found. Is /dev/vendor the expected filesystem?" >&2
    exit 1
}

printf '[4/5] Replacing cfg_fp.ini and syncing /dev/vendor...\n'
cp "$CFG_TMP" "$STAGE"
chmod 0644 "$STAGE"
mv -f "$STAGE" "$CFG_TARGET"
cmp -s "$CFG_TMP" "$CFG_TARGET" || {
    echo 'Configuration copy verification failed.' >&2
    exit 1
}
sync
umount "$CE_DIR"
MOUNTED=0

printf '[5/5] Flashing DTB to /dev/dtb...\n'
dd if="$DTB_TMP" of=/dev/dtb bs=4096
sync
printf '\nSUCCESS: cfg_fp.ini replaced, DTB flashed, vendor unmounted.\n'
printf 'Reboot when ready to apply the changes.\n'
