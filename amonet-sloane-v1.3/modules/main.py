import struct
import sys
import os

from common import Device
from handshake import handshake
from load_payload import load_payload
from logger import log
import glob
import time

def check_modemmanager():
    pids = [pid for pid in os.listdir('/proc') if pid.isdigit()]

    for pid in pids:
        try:
            args = open(os.path.join('/proc', pid, 'cmdline'), 'rb').read().decode("utf-8").split('\0')
            if len(args) > 0 and "modemmanager" in args[0].lower():
                print("You need to temporarily disable/uninstall ModemManager before this script can proceed")
                sys.exit(1)
        except IOError:
            continue

def switch_boot0(dev):
    dev.emmc_switch(1)
    block = dev.emmc_read(0)
    if block[0:9] != b"EMMC_BOOT" and block != b"\x00" * 0x200:
        dev.reboot()
        raise RuntimeError("what's wrong with your BOOT0?")

def flash_data(dev, data, start_block, max_size=0):
    while len(data) % 0x200 != 0:
        data += b"\x00"

    if max_size and len(data) > max_size:
        raise RuntimeError("data too big to flash")

    blocks = len(data) // 0x200
    for x in range(blocks):
        print("[{} / {}]".format(x + 1, blocks), end='\r')
        dev.emmc_write(start_block + x, data[x * 0x200:(x + 1) * 0x200])
    print("")

def flash_binary(dev, path, start_block, max_size=0):
    with open(path, "rb") as fin:
        data = fin.read()
    while len(data) % 0x200 != 0:
        data += b"\x00"

    if max_size and len(data) > max_size:
        raise RuntimeError("data too big to flash")

    blocks = len(data) // 0x200
    for x in range(blocks):
        print("[{} / {}]".format(x + 1, blocks), end='\r')
        dev.emmc_write(start_block + x, data[x * 0x200:(x + 1) * 0x200])
    print("")

def switch_user(dev):
    dev.emmc_switch(0)
    block = dev.emmc_read(0)
    if block[510:512] != b"\x55\xAA":
        dev.reboot()
        raise RuntimeError("what's wrong with your GPT?")

def parse_gpt(dev):
    data = dev.emmc_read(0x400 // 0x200) + dev.emmc_read(0x600 // 0x200) + dev.emmc_read(0x800 // 0x200) + dev.emmc_read(0xA00 // 0x200)
    num = len(data) // 0x80
    parts = dict()
    for x in range(num):
        part = data[x * 0x80:(x + 1) * 0x80]
        part_name = part[0x38:].decode("utf-16le").rstrip("\x00")
        part_start = struct.unpack("<Q", part[0x20:0x28])[0]
        part_end = struct.unpack("<Q", part[0x28:0x30])[0]
        parts[part_name] = (part_start, part_end - part_start + 1)
    return parts

def main():
    dev = Device()
    dev.find_device()

    # 0.1) Handshake
    handshake(dev)

    # 0.2) Load brom payload
    load_payload(dev, "../brom-payload/build/payload.bin")

    if len(sys.argv) == 2 and sys.argv[1] == "gpt-fix":
        dev.emmc_switch(0)
        log("Flashing GPT...")
        flash_binary(dev, "../bin/gpt-sloane.bin", 0, 34 * 0x200)

    # 1) Sanity check GPT
    log("Check GPT")
    switch_user(dev)

    # 1.1) Parse gpt
    gpt = parse_gpt(dev)
    log("gpt_parsed = {}".format(gpt))
    if "lk" not in gpt or "TEE1" not in gpt or "boot" not in gpt or "recovery" not in gpt or "system" not in gpt or "cache" not in gpt:
        raise RuntimeError("bad gpt")

    # 2) Sanity check boot0
    log("Check boot0")
    switch_boot0(dev)

    # 3) Sanity check rpmb
    log("Check rpmb")
    rpmb = dev.rpmb_read()
    log("RPMB: {}".format(rpmb))

    # 4) Clear preloader so, we get into bootrom without shorting, should the script stall (we flash preloader as last step)
    log("Clear preloader header")
    switch_boot0(dev)
    flash_data(dev, b"EMMC_BOOT" + b"\x00" * ((0x200 * 8) - 9), 0)

    # 5) Downgrade RPMB
    # RPMB key-derivation needs more research in sloane. You can only boot old bootloaders (i.e: 5.0.5) without downgrade RPMB.
    # Flashing old PL/TZ will result in a MT65xx Preloader brick.
    #log("Downgrade rpmb")
    #dev.rpmb_write(b"\x00" * 0x100)

    # 6) Flash tz
    log("Flashing TEE..")
    switch_user(dev)
    flash_binary(dev, "../bin/tz.img", gpt["TEE1"][0], gpt["TEE1"][1] * 0x200)

    # 7) Flash lk
    log("Flashing bootloader..")
    switch_user(dev)
    flash_binary(dev, "../bin/lk.img", gpt["lk"][0], gpt["lk"][1] * 0x200)

    # 8) Flash unbrick.img
    log("Flashing unbrick image..")
    switch_user(dev)
    flash_binary(dev, "../bin/unbrick.img", gpt["system"][0], gpt["system"][1] * 0x200)
   
    # 9) Flash boot
    log("Flashing boot..")
    switch_user(dev)
    flash_binary(dev, "../bin/boot.img", gpt["boot"][0], gpt["boot"][1] * 0x200)

    # 10) Force recovery
    log("Forcing recovery in next reboot..")
    switch_user(dev)
    flash_binary(dev, "../bin/force_recovery.img", gpt["cache"][0], gpt["cache"][1] * 0x200)

    # 11) Flash preloader
    log("Flashing preloader..")
    switch_boot0(dev)
    flash_binary(dev, "../bin/preloader.img", 0)

    # 11.1) Wait some time so data is flushed to EMMC
    time.sleep(5)

    # 12) Reboot
    log("Reboot to TWRP..")
    dev.reboot()

if __name__ == "__main__":
    if os.geteuid() != 0:
         raise RuntimeError("must run as root")
    check_modemmanager()
    main()
