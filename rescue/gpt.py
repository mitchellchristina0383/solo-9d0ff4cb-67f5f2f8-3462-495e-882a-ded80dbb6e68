"""GPT 分区表的只读核验与解析。

设计约束
--------
- 只基于“当前已经救出的扇区”工作：``read_sector(lba)`` 返回 512 字节或
  ``None``（该扇区尚未救出）。模块本身不做任何磁盘 I/O。
- 支持标准 GPT 规格：128 个分区项、每项 128 字节（分区项数组 16384 字节，
  占 32 个扇区）。头中声明的项数允许 1..128，项大小必须为 128。
- 主、备两份 GPT 分别核验：表头 CRC32（校验时表头 CRC 字段清零，覆盖
  header_size 字节）与分区项数组 CRC32（头中 num_entries * entry_size 字节）。
- CRC 通过后检查分区边界：起止 LBA 合法、不越盘、落在 first/last usable
  LBA 区间内、分区之间不重叠。

决策规则（由 :func:`analyze_disk` 实现）
----------------------------------------
- 只有一份有效 -> 采用该份（另一份失败/缺失的原因仍返回，供页面说明）。
- 两份都有效且布局一致 -> 采用主表。
- 两份都有效但布局不同 -> ``status="conflict"``，停止提取并给出差异说明。
- 两份都无法通过核验 -> ``status="invalid"``（均已完整读出但核验失败）或
  ``status="incomplete"``（至少一份尚缺扇区，可补读后再核验），不输出分区。
"""

from __future__ import annotations

import struct
import zlib
from typing import Callable, Dict, List, Optional, Set

SECTOR_SIZE = 512

GPT_SIGNATURE = b"EFI PART"
PRIMARY_HEADER_LBA = 1

#: 本工具支持的固定规格：128 项 × 128 字节。
SUPPORTED_ENTRY_COUNT = 128
ENTRY_SIZE = 128
ENTRY_ARRAY_BYTES = SUPPORTED_ENTRY_COUNT * ENTRY_SIZE  # 16384
ENTRY_ARRAY_SECTORS = ENTRY_ARRAY_BYTES // SECTOR_SIZE  # 32

ReadSector = Callable[[int], Optional[bytes]]

# 常见分区类型 GUID 的中文名（未列出的 GUID 仍照常提取原始字节）。
KNOWN_TYPE_NAMES: Dict[str, str] = {
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EFI 系统分区",
    "0FC63DAF-8483-4772-8E79-3D69D8477DE4": "Linux 文件系统",
    "44479540-F297-41B2-9AF7-D131D5F0458A": "Linux x86 根分区",
    "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709": "Linux x86-64 根分区",
    "A19D880F-05FC-4D3B-A006-743F0F84911E": "Linux RAID",
    "E6D6D379-F507-44C2-A23C-238F2A3DF928": "Linux LVM",
    "933AC7E1-2EB4-4F13-B844-0E14E2AEF915": "Linux /home",
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "Microsoft 基本数据",
    "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": "Windows 恢复环境",
    "21686148-6449-6E6F-744E-656564454649": "BIOS boot 分区",
}


def format_guid(raw: bytes) -> str:
    """把 16 字节混合端序 GUID 转成标准 8-4-4-4-12 大写字符串。"""
    if len(raw) != 16:
        raise ValueError("GUID 必须为 16 字节")
    d1, d2, d3 = struct.unpack_from("<IHH", raw, 0)
    tail = raw[8:16]
    return (
        f"{d1:08X}-{d2:04X}-{d3:04X}-"
        f"{tail[0]:02X}{tail[1]:02X}-"
        f"{tail[2]:02X}{tail[3]:02X}{tail[4]:02X}{tail[5]:02X}"
        f"{tail[6]:02X}{tail[7]:02X}"
    )


def guid_to_bytes(text: str) -> bytes:
    """:func:`format_guid` 的逆运算，供构造样盘/测试使用。"""
    parts = text.strip().split("-")
    if len(parts) != 5 or [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        raise ValueError(f"GUID 格式不正确：{text!r}")
    d1 = int(parts[0], 16)
    d2 = int(parts[1], 16)
    d3 = int(parts[2], 16)
    tail = bytes.fromhex(parts[3] + parts[4])
    return struct.pack("<IHH", d1, d2, d3) + tail


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _fallback_geometry(role: str, total_sectors: int):
    """表头不可信/缺失时按标准固定位置估算结构范围。

    返回 (header_lba, entry_start_lba, entry_sector_count)。
    """
    if role == "primary":
        return PRIMARY_HEADER_LBA, 2, ENTRY_ARRAY_SECTORS
    header_lba = total_sectors - 1
    return header_lba, header_lba - ENTRY_ARRAY_SECTORS, ENTRY_ARRAY_SECTORS


def _standard_missing(
    role: str, total_sectors: int, read_sector: ReadSector
) -> List[int]:
    """按标准几何列出主/备 GPT 结构中当前读不到的扇区。"""
    header_lba, entry_start, count = _fallback_geometry(role, total_sectors)
    candidates = [header_lba] + list(range(entry_start, entry_start + count))
    missing: Set[int] = set()
    for lba in candidates:
        if 0 <= lba < total_sectors and read_sector(lba) is None:
            missing.add(lba)
    return sorted(missing)


def _parse_partition_entry(entry: bytes, index: int) -> Optional[dict]:
    type_raw = entry[0:16]
    if type_raw == b"\x00" * 16:
        return None
    first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
    attributes = struct.unpack_from("<Q", entry, 48)[0]
    name = entry[56:128].decode("utf-16-le", errors="replace").rstrip("\x00")
    type_guid = format_guid(type_raw)
    return {
        "index": index,
        "type_guid": type_guid,
        "type_name": KNOWN_TYPE_NAMES.get(type_guid, "未知类型"),
        "unique_guid": format_guid(entry[16:32]),
        "name": name,
        "first_lba": first_lba,
        "last_lba": last_lba,
        "sector_count": (
            last_lba - first_lba + 1 if last_lba >= first_lba else 0
        ),
        "size_bytes": (
            (last_lba - first_lba + 1) * SECTOR_SIZE
            if last_lba >= first_lba
            else 0
        ),
        "attributes": attributes,
    }


def analyze_copy(
    role: str, total_sectors: int, read_sector: ReadSector
) -> dict:
    """核验一份 GPT（role="primary" 或 "backup"）。

    返回可直接 JSON 序列化的诊断字典，``status`` 为
    ``valid`` / ``invalid`` / ``incomplete`` 之一。
    """
    is_backup = role == "backup"
    label = "备 GPT" if is_backup else "主 GPT"
    fallback_header, _, _ = _fallback_geometry(role, total_sectors)
    diag: dict = {
        "role": role,
        "label": label,
        "header_lba": fallback_header,
        "status": "invalid",
        "reasons": [],
        "missing_sectors": [],
        "header_crc_ok": None,
        "entries_crc_ok": None,
        "num_entries": None,
        "entry_size": None,
        "entry_lba": None,
        "partitions": [],
    }

    if total_sectors <= 1:
        diag["reasons"].append(f"{label}：磁盘扇区数 {total_sectors} 太少，无法容纳 GPT")
        return diag

    # 1) 表头扇区 ------------------------------------------------------- #
    header = read_sector(fallback_header)
    if header is None:
        diag["status"] = "incomplete"
        diag["missing_sectors"] = _standard_missing(
            role, total_sectors, read_sector
        )
        diag["reasons"].append(
            f"{label}表头扇区 LBA {fallback_header} 尚未救出，无法核验"
        )
        return diag
    if len(header) != SECTOR_SIZE:
        diag["reasons"].append(
            f"{label}表头扇区长度异常：{len(header)}（应为 {SECTOR_SIZE}）"
        )
        return diag

    if header[:8] != GPT_SIGNATURE:
        diag["reasons"].append(
            f"{label}表头签名错误：{header[:8]!r}（应为 {GPT_SIGNATURE!r}）"
        )
        # 签名坏时字段不可信，仍按标准位置报缺扇区，方便补读。
        diag["missing_sectors"] = _standard_missing(
            role, total_sectors, read_sector
        )
        return diag

    try:
        _revision, header_size, header_crc_stored = struct.unpack_from(
            "<III", header, 8
        )
    except struct.error:
        diag["reasons"].append(f"{label}表头长度不足，无法解析 CRC 字段")
        return diag

    if header_size < 92 or header_size > SECTOR_SIZE:
        diag["reasons"].append(
            f"{label}表头大小字段非法：header_size={header_size}（合法范围 92..512）"
        )
        diag["missing_sectors"] = _standard_missing(
            role, total_sectors, read_sector
        )
        return diag

    crc_buf = bytearray(header[:header_size])
    struct.pack_into("<I", crc_buf, 16, 0)  # 校验时 CRC 字段本身清零
    header_crc_calc = _crc32(bytes(crc_buf))
    diag["header_crc_ok"] = header_crc_calc == header_crc_stored
    if not diag["header_crc_ok"]:
        diag["reasons"].append(
            f"{label}表头 CRC32 核验失败：表内 0x{header_crc_stored:08X}，"
            f"实算 0x{header_crc_calc:08X}"
        )
        diag["missing_sectors"] = _standard_missing(
            role, total_sectors, read_sector
        )
        return diag

    # 2) 表头字段（CRC 已通过，可信任）---------------------------------- #
    current_lba = struct.unpack_from("<Q", header, 24)[0]
    backup_lba = struct.unpack_from("<Q", header, 32)[0]
    first_usable = struct.unpack_from("<Q", header, 40)[0]
    last_usable = struct.unpack_from("<Q", header, 48)[0]
    entry_lba = struct.unpack_from("<Q", header, 72)[0]
    num_entries = struct.unpack_from("<I", header, 80)[0]
    entry_size = struct.unpack_from("<I", header, 84)[0]
    entries_crc_stored = struct.unpack_from("<I", header, 88)[0]

    diag.update(
        {
            "current_lba": current_lba,
            "backup_lba": backup_lba,
            "first_usable_lba": first_usable,
            "last_usable_lba": last_usable,
            "entry_lba": entry_lba,
            "num_entries": num_entries,
            "entry_size": entry_size,
        }
    )

    geometry_errors: List[str] = []
    if current_lba != fallback_header:
        geometry_errors.append(
            f"{label}表头 currentLBA={current_lba} 与实际位置 LBA {fallback_header} 不符"
        )
    if first_usable > last_usable:
        geometry_errors.append(
            f"{label}可用 LBA 区间非法：{first_usable}..{last_usable}"
        )
    if entry_size != ENTRY_SIZE:
        geometry_errors.append(
            f"{label}仅支持每项 {ENTRY_SIZE} 字节，表头声明 {entry_size} 字节"
        )
    if not (1 <= num_entries <= SUPPORTED_ENTRY_COUNT):
        geometry_errors.append(
            f"{label}仅支持 1..{SUPPORTED_ENTRY_COUNT} 个分区项，"
            f"表头声明 {num_entries} 个"
        )
    byte_len = num_entries * entry_size
    entry_sector_count = (byte_len + SECTOR_SIZE - 1) // SECTOR_SIZE
    if entry_lba == 0 or entry_lba >= total_sectors:
        geometry_errors.append(
            f"{label}分区项数组起始 LBA {entry_lba} 越界（全盘 {total_sectors} 扇区）"
        )
    elif entry_lba + entry_sector_count > total_sectors:
        geometry_errors.append(
            f"{label}分区项数组延伸到 LBA {entry_lba + entry_sector_count - 1}，"
            f"超出盘尾 {total_sectors - 1}"
        )

    if geometry_errors:
        diag["reasons"].extend(geometry_errors)
        diag["missing_sectors"] = _standard_missing(
            role, total_sectors, read_sector
        )
        return diag

    # 3) 分区项数组是否已全部救出 + CRC32 ------------------------------- #
    missing_entries: List[int] = []
    chunks: List[bytes] = []
    for offset in range(entry_sector_count):
        lba = entry_lba + offset
        sector = read_sector(lba)
        if sector is None:
            missing_entries.append(lba)
        else:
            chunks.append(sector)

    if missing_entries:
        diag["status"] = "incomplete"
        diag["missing_sectors"] = sorted(missing_entries)
        diag["reasons"].append(
            f"{label}分区项数组（{num_entries} 项 × {entry_size} 字节）"
            f"尚缺 {len(missing_entries)} 个扇区："
            f"{_preview(missing_entries)}"
        )
        return diag

    entry_array = b"".join(chunks)[:byte_len]
    entries_crc_calc = _crc32(entry_array)
    diag["entries_crc_ok"] = entries_crc_calc == entries_crc_stored
    if not diag["entries_crc_ok"]:
        diag["reasons"].append(
            f"{label}分区项数组 CRC32 核验失败：表内 0x{entries_crc_stored:08X}，"
            f"实算 0x{entries_crc_calc:08X}"
        )
        return diag

    # 4) 解析分区项并检查边界 ------------------------------------------- #
    partitions: List[dict] = []
    boundary_errors: List[str] = []
    for index in range(num_entries):
        entry = entry_array[index * entry_size : (index + 1) * entry_size]
        parsed = _parse_partition_entry(entry, index)
        if parsed is None:
            continue
        partitions.append(parsed)

        first_lba = parsed["first_lba"]
        last_lba = parsed["last_lba"]
        desc = f"{label}第 {index + 1} 项（{parsed['unique_guid']}）"
        if first_lba > last_lba:
            boundary_errors.append(
                f"{desc}边界非法：起始 LBA {first_lba} 大于结束 LBA {last_lba}"
            )
            continue
        if last_lba >= total_sectors:
            boundary_errors.append(
                f"{desc}结束 LBA {last_lba} 超出盘尾 {total_sectors - 1}"
            )
        if first_lba < first_usable or last_lba > last_usable:
            boundary_errors.append(
                f"{desc}范围 {first_lba}..{last_lba} 越过 GPT 可用区间 "
                f"{first_usable}..{last_usable}"
            )

    # 分区两两不得重叠（相邻不算重叠）。
    ordered = sorted(partitions, key=lambda p: p["first_lba"])
    for prev, cur in zip(ordered, ordered[1:]):
        if prev["first_lba"] > prev["last_lba"]:
            continue
        if cur["first_lba"] <= prev["last_lba"]:
            boundary_errors.append(
                f"{label}第 {prev['index'] + 1} 项与第 {cur['index'] + 1} 项重叠："
                f"{prev['first_lba']}..{prev['last_lba']} 与 "
                f"{cur['first_lba']}..{cur['last_lba']}"
            )

    diag["partitions"] = partitions
    if boundary_errors:
        diag["reasons"].extend(boundary_errors)
        return diag

    diag["status"] = "valid"
    return diag


def _fingerprint(diag: dict) -> list:
    """布局指纹：槽位 + 类型/唯一 GUID + 起止 LBA。"""
    return [
        (
            p["index"],
            p["type_guid"],
            p["unique_guid"],
            p["first_lba"],
            p["last_lba"],
        )
        for p in diag["partitions"]
    ]


def _diff_layouts(primary: dict, backup: dict) -> List[str]:
    a = {p["index"]: p for p in primary["partitions"]}
    b = {p["index"]: p for p in backup["partitions"]}
    diffs: List[str] = []
    for index in sorted(set(a) | set(b)):
        pa, pb = a.get(index), b.get(index)
        if pb is None:
            diffs.append(
                f"分区项 {index + 1}：仅主表列出 "
                f"（{pa['first_lba']}..{pa['last_lba']}，{pa['unique_guid']}）"
            )
            continue
        if pa is None:
            diffs.append(
                f"分区项 {index + 1}：仅备表列出 "
                f"（{pb['first_lba']}..{pb['last_lba']}，{pb['unique_guid']}）"
            )
            continue
        for key, zh in (
            ("type_guid", "类型 GUID"),
            ("unique_guid", "唯一 GUID"),
            ("first_lba", "起始扇区"),
            ("last_lba", "结束扇区"),
        ):
            if pa[key] != pb[key]:
                diffs.append(
                    f"分区项 {index + 1} {zh}不一致："
                    f"主表 {pa[key]} / 备表 {pb[key]}"
                )
    return diffs


def _annotate_missing(diag: dict, missing_set: Optional[Set[int]]) -> None:
    """给每个分区补上其范围内当前仍缺失的绝对扇区编号。"""
    for part in diag.get("partitions", []):
        if missing_set is None:
            part["missing_sectors"] = []
            part["missing_count"] = 0
            part["complete"] = True
            continue
        missing = [
            lba
            for lba in range(part["first_lba"], part["last_lba"] + 1)
            if lba in missing_set
        ]
        part["missing_sectors"] = missing
        part["missing_count"] = len(missing)
        part["complete"] = not missing


def analyze_disk(
    total_sectors: int,
    read_sector: ReadSector,
    missing_set: Optional[Set[int]] = None,
) -> dict:
    """分析整块盘的主/备 GPT 并给出提取决策。

    ``missing_set`` 为当前全盘仍缺失的绝对扇区号集合；提供时会给每个分区
    标注 ``missing_sectors``。
    """
    primary = analyze_copy("primary", total_sectors, read_sector)
    backup = analyze_copy("backup", total_sectors, read_sector)
    _annotate_missing(primary, missing_set)
    _annotate_missing(backup, missing_set)

    result: dict = {
        "sector_count": total_sectors,
        "status": "invalid",
        "source": None,
        "source_label": None,
        "reasons": [],
        "differences": [],
        "primary": primary,
        "backup": backup,
        "partitions": [],
    }

    copies = [primary, backup]
    valid = [c for c in copies if c["status"] == "valid"]

    if len(valid) == 2:
        if _fingerprint(primary) == _fingerprint(backup):
            result["status"] = "ok"
            result["source"] = "primary"
            result["source_label"] = "主 GPT"
            result["partitions"] = primary["partitions"]
            result["reasons"].append("主、备 GPT 均有效且布局一致，采用主表")
        else:
            result["status"] = "conflict"
            result["differences"] = _diff_layouts(primary, backup)
            result["reasons"].append(
                "主、备 GPT 均通过 CRC32 与边界核验，但两份布局不同；"
                "无法判断哪一份代表当前分区，已停止提取。差异："
                + "；".join(result["differences"])
            )
        return result

    if len(valid) == 1:
        chosen = valid[0]
        other = backup if chosen is primary else primary
        result["status"] = "ok"
        result["source"] = chosen["role"]
        result["source_label"] = chosen["label"]
        result["partitions"] = chosen["partitions"]
        if other["status"] == "valid":
            # 理论上不会进此分支（两个都 valid 已在上面处理），防御性保留。
            pass
        elif other["status"] == "incomplete":
            result["reasons"].append(
                f"仅有{chosen['label']}通过核验；另一份（{other['label']}）"
                f"尚缺扇区无法核验：{'; '.join(other['reasons'])}。"
                f"按规则使用{chosen['label']}提取。"
            )
        else:
            result["reasons"].append(
                f"仅有{chosen['label']}通过核验；另一份（{other['label']}）"
                f"核验失败：{'; '.join(other['reasons'])}。"
                f"按规则使用{chosen['label']}提取。"
            )
        return result

    # 两份都不 valid ----------------------------------------------------- #
    if any(c["status"] == "incomplete" for c in copies):
        result["status"] = "incomplete"
        result["reasons"].append(
            "主、备 GPT 目前都无法通过核验：至少一份还缺少扇区，"
            "请按下面列出的编号补读后重新分析"
        )
    else:
        result["status"] = "invalid"
        result["reasons"].append(
            "主、备 GPT 均已读出但都未通过核验，已停止提取："
            + " | ".join(
                f"{c['label']}：{'; '.join(c['reasons'])}" for c in copies
            )
        )
    return result


def _preview(numbers: List[int], limit: int = 20) -> str:
    shown = ",".join(str(n) for n in numbers[:limit])
    if len(numbers) > limit:
        shown += f" ...（共 {len(numbers)} 个）"
    return shown
