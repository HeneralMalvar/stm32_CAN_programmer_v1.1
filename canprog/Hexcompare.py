import sys

def parse_intel_hex(path):
    memory = {}
    upper = 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith(":"):
                continue

            count = int(line[1:3], 16)
            addr = int(line[3:7], 16)
            rectype = int(line[7:9], 16)
            data = line[9:9 + count * 2]

            if rectype == 0x00:
                base = upper + addr
                for i in range(count):
                    memory[base + i] = int(data[i*2:i*2+2], 16)

            elif rectype == 0x04:
                upper = int(data, 16) << 16

            elif rectype == 0x01:
                break

    return memory

if len(sys.argv) != 3:
    print("Usage: python compare_hex.py original.hex readback.hex")
    sys.exit(1)

original = parse_intel_hex(sys.argv[1])
readback = parse_intel_hex(sys.argv[2])

missing = []
mismatch = []

for addr, value in original.items():
    if addr not in readback:
        missing.append(addr)
    elif readback[addr] != value:
        mismatch.append((addr, value, readback[addr]))

if not missing and not mismatch:
    print("PASS: Readback matches original HEX.")
else:
    print("FAIL: Readback does not match original HEX.")

    if missing:
        print(f"Missing addresses: {len(missing)}")
        print(f"First missing: 0x{missing[0]:08X}")

    if mismatch:
        addr, exp, got = mismatch[0]
        print(f"Mismatches: {len(mismatch)}")
        print(f"First mismatch at 0x{addr:08X}: expected 0x{exp:02X}, got 0x{got:02X}")