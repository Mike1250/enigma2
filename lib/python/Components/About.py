from fcntl import ioctl
from os import path as ospath
from socket import AF_INET, SOCK_DGRAM, inet_ntoa, socket
from struct import pack
from sys import modules
from time import time
from Tools.Directories import fileExists

from platform import machine



def getCPUArch(MODEL):
    architecture = checkCPUArch()
    cpu_arch = architecture if "ARM" in architecture else _("Mipsel")
    return [cpu_arch, getCPUSpeedString(MODEL), getCpuCoresString()]


def checkCPUArch():
    # /proc/cpuinfo often has only 'processor: 0' etc. on Amlogic ARM64.
    # Prefer the kernel's machine architecture and never index an empty list.
    arch = machine().strip().lower()
    if arch in ("aarch64", "arm64"):
        return "ARM64"
    if arch.startswith("arm"):
        return "ARM" + arch[3:]

    try:
        if fileExists("/proc/cpuinfo"):
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as info:
                for line in info:
                    key, separator, value = line.partition(":")
                    if not separator or key.strip().lower() not in ("system type", "model name", "processor"):
                        continue
                    tokens = value.strip().split()
                    if not tokens or tokens[0].isdigit():
                        continue
                    cpu = tokens[0]
                    if cpu.lower() in ("aarch64", "arm64"):
                        return "ARM64"
                    if cpu.lower().startswith("arm"):
                        return "ARM" + cpu[3:]
                    return cpu
    except OSError as error:
        print("[About] CPU detection error:", error)

    if arch.startswith("mips"):
        return "MIPS"
    return arch.upper() if arch else _("unavailable")


def getCPUSpeedString(MODEL):
    speed = float(getCPUSpeedMHzInt(MODEL))
    if speed > 0:
        if speed >= 1000:
            return f"{round(speed / 1000, 1)} GHz"
        return f"{int(speed)} MHz"
    return _("unknown")


def getCPUSpeedMHzInt(MODEL):
    speed = 0
    try:
        with open("/proc/cpuinfo", "r") as info:
            for line in info:
                key, separator, value = line.partition(":")
                if separator and key.strip().lower() == "cpu mhz":
                    speed = float(value.strip().split()[0])
                    break
    except (OSError, ValueError, IndexError):
        print("[About] CPU MHz not available from /proc/cpuinfo")

    if not speed and MODEL in ("h7", "h17", "sf4008", "osmio4kplus", "osmini4k"):
        try:
            with open("/sys/firmware/devicetree/base/cpus/cpu@0/clock-frequency", "rb") as file:
                speed = int.from_bytes(file.read(), byteorder="big") / 1000000
        except OSError:
            speed = 1700

    if not speed and MODEL in ("h8", "sfx6008", "sfx6018"):
        speed = 1200

    if not speed:
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "r") as file:
                speed = float(file.read().strip()) / 1000
        except (OSError, ValueError):
            print("[About] CPU max frequency not available")
    return int(speed)


def getCpuCoresString():
    cores = getCpuCoresInt()
    return {
        0: _("Unavailable"),
        1: _("Single Core"),
        2: _("Dual Core"),
        4: _("Quad Core"),
        8: _("Octo Core")
    }.get(cores, _("%d cores") % cores)


def getCpuCoresInt():
    try:
        with open("/sys/devices/system/cpu/present", "r") as file:
            value = file.read().strip()
        total = 0
        for part in value.split(","):
            part = part.strip()
            if "-" in part:
                start, end = (int(n) for n in part.split("-", 1))
                if end < start:
                    return 0
                total += end - start + 1
            elif part:
                int(part)
                total += 1
        return total
    except (OSError, ValueError):
        return 0


def _ifinfo(sock, addr, ifname):
    iface = pack("256s", bytes(ifname[:15], "utf-8"))
    info = ioctl(sock.fileno(), addr, iface)
    if addr == 0x8927:
        return ":".join(f"{char:02X}" for char in info[18:24])
    return inet_ntoa(info[20:24])


def getIfConfig(ifname):
    ifreq = {"ifname": ifname}
    infos = {
        "addr": 0x8915,
        "brdaddr": 0x8919,
        "hwaddr": 0x8927,
        "netmask": 0x891b
    }
    sock = socket(AF_INET, SOCK_DGRAM)
    try:
        for key, value in infos.items():
            ifreq[key] = _ifinfo(sock, value, ifname)
    except Exception as error:
        print(f"[About] getIfConfig: {error}")
    finally:
        sock.close()
    print(f"[About] ifreq:{ifreq}")
    return ifreq


def getIfTransferredData(ifname):
    try:
        with open("/proc/net/dev", "r") as file:
            for line in file:
                if line.lstrip().startswith(ifname + ":"):
                    values = line.split(":", 1)[1].split()
                    return values[0], values[8]
    except (OSError, ValueError, IndexError):
        pass
    return "0", "0"


def getBoxUptime():
    try:
        with open("/proc/uptime", "rb") as file:
            seconds = int(file.readline().split(b".")[0])
        return formatUptime(seconds)
    except (OSError, ValueError, IndexError):
        return ""


def getEnigmaUptime():
    try:
        seconds = int(time() - ospath.getmtime("/etc/enigma2/profile"))
        return formatUptime(seconds)
    except (OSError, ValueError):
        return ""


def formatUptime(seconds):
    out = ""
    if seconds > 86400:
        days = int(seconds / 86400)
        out += (_("1 day") if days == 1 else _("%d days") % days) + ", "
    if seconds > 3600:
        hours = int((seconds % 86400) / 3600)
        out += (_("1 hour") if hours == 1 else _("%d hours") % hours) + ", "
    if seconds > 60:
        minutes = int((seconds % 3600) / 60)
        out += (_("1 minute") if minutes == 1 else _("%d minutes") % minutes) + " "
    else:
        out += (_("1 second") if seconds == 1 else _("%d seconds") % seconds) + " "
    return out


# For modules that do "from About import about"
about = modules[__name__]
