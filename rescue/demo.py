"""构造可在网页直接演示的 GPT 样盘。

这一个模块同时服务三处，保证“所见即所测”：

1. ``GET /api/demo/sample.img`` 下载的样盘；
2. ``POST /api/demo/setup`` 一键建任务并模拟首轮读盘；
3. ``scripts/make_sample.py`` 落盘的 ``samples/sample-gpt.img``，
   可配合 ``scripts/sim_reader.py`` 走真实 HTTP 演练。

样盘特意埋了一个“主备分歧”场景
------------------------------
- 备 GPT（盘尾，首轮已救出）：3 个分区，布局 A；
- 主 GPT（LBA 1..33，首轮故意不救）：CRC 与边界全部合法，但布局不同
  （第 2 项更短、没有第 3 项）。

因此首轮分析必然“仅凭备表提取”；把主 GPT 扇区补读回来后，两份都有效
但布局冲突，工具按规则停止提取。此外在分区内部埋了几个首轮读不出的
坏扇区，用于演示“列出缺失编号 -> 补读 -> 下载原始字节”。
"""

from __future__ import annotations

import struct
from typing import List, Optional, Sequence, Tuple

from .gpt import ENTRY_ARRAY_BYTES, SECTOR_SIZE, guid_to_bytes

#: 演示盘 1024 扇区 = 512 KiB，建任务时逐扇区 fsync 也只需约 2 秒。
DEMO_SECTOR_COUNT = 1024

EFI_TYPE_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX_TYPE_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"

# (槽位 0 起, 类型 GUID, 唯一 GUID, 起始 LBA, 结束 LBA(含), 名称)
PartitionSpec = Tuple[int, str, str, int, int, str]

DEMO_DISK_GUID = "BAAD0001-0000-4000-8000-000000000001"

# 备表：完整的 3 分区布局（权威布局 A）。
BACKUP_SPECS: Sequence[PartitionSpec] = (
    (0, EFI_TYPE_GUID, "11111111-1111-4111-8111-111111111111", 64, 191, "EFI"),
    (
        1,
        LINUX_TYPE_GUID,
        "22222222-2222-4222-8222-222222222222",
        192,
        511,
        "rescue-root",
    ),
    (
        2,
        LINUX_TYPE_GUID,
        "33333333-3333-4333-8333-333333333333",
        512,
        990,
        "rescue-data",
    ),
)

# 主表：合法但陈旧/分歧的布局 B —— 第 2 分区更短，且没有第 3 分区。
PRIMARY_SPECS: Sequence[PartitionSpec] = (
    (0, EFI_TYPE_GUID, "11111111-1111-4111-8111-111111111111", 64, 191, "EFI"),
    (
        1,
        LINUX_TYPE_GUID,
        "22222222-2222-4222-8222-222222222222",
        192,
        383,
        "rescue-root",
    ),
)

#: 首轮故意不救的主 GPT 扇区（表头 + 32 个分区项扇区）。
DEMO_PRIMARY_GPT_SECTORS: List[int] = list(range(1, 34))

#: 各分区内部首轮“读失败”的扇区（绝对 LBA）。
DEMO_BAD_SECTORS: List[int] = [100, 250, 400, 700, 900]

#: 一键演示首轮跳过的全部扇区。
DEMO_FIRST_ROUND_SKIP: List[int] = sorted(
    set(DEMO_PRIMARY_GPT_SECTORS) | set(DEMO_BAD_SECTORS)
)


def build_entry(spec: PartitionSpec) -> bytes:
    """构造一个 128 字节 GPT 分区项。"""
    index, type_guid, unique_guid, first_lba, last_lba, name = spec
    entry = bytearray(128)
    entry[0:16] = guid_to_bytes(type_guid)
    entry[16:32] = guid_to_bytes(unique_guid)
    struct.pack_into("<Q", entry, 32, first_lba)
    struct.pack_into("<Q", entry, 40, last_lba)
    struct.pack_into("<Q", entry, 48, 0)  # attributes
    encoded = name.encode("utf-16-le")[:72]
    entry[56 : 56 + len(encoded)] = encoded
    return bytes(entry)


def build_entry_array(specs: Sequence[PartitionSpec]) -> bytes:
    """构造固定 128 槽 × 128 字节的分区项数组（16384 字节）。"""
    entries = [b"\x00" * 128 for _ in range(128)]
    for spec in specs:
        entries[spec[0]] = build_entry(spec)
    array = b"".join(entries)
    assert len(array) == ENTRY_ARRAY_BYTES
    return array


def build_header_sector(
    *,
    header_lba: int,
    alternate_lba: int,
    entry_lba: int,
    entries_crc: int,
    disk_guid: str = DEMO_DISK_GUID,
    first_usable: int = 34,
    last_usable: Optional[int] = None,
    total_sectors: int = DEMO_SECTOR_COUNT,
) -> bytes:
    """构造一个带正确表头 CRC32 的 512 字节 GPT 表头扇区。"""
    if last_usable is None:
        last_usable = total_sectors - 34
    header = bytearray(SECTOR_SIZE)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)   # revision 1.0
    struct.pack_into("<I", header, 12, 92)          # header size
    struct.pack_into("<I", header, 16, 0)           # CRC 占位，稍后回填
    struct.pack_into("<I", header, 20, 0)           # reserved
    struct.pack_into("<Q", header, 24, header_lba)
    struct.pack_into("<Q", header, 32, alternate_lba)
    struct.pack_into("<Q", header, 40, first_usable)
    struct.pack_into("<Q", header, 48, last_usable)
    header[56:72] = guid_to_bytes(disk_guid)
    struct.pack_into("<Q", header, 72, entry_lba)
    struct.pack_into("<I", header, 80, 128)         # num entries
    struct.pack_into("<I", header, 84, 128)         # entry size
    struct.pack_into("<I", header, 88, entries_crc)
    import zlib

    header_crc = zlib.crc32(bytes(header[:92])) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, header_crc)
    return bytes(header)


def _fill_partition_content(
    image: bytearray, specs: Sequence[PartitionSpec], total_sectors: int
) -> None:
    """给每个分区填确定性图案，并在首扇区放可读标记。

    同唯一 GUID 的分区（主备表里的同一个分区）填充完全一致，便于核对。
    """
    for spec in specs:
        _idx, _type, unique_guid, first, last, name = spec
        if first > last or first < 0 or last >= total_sectors:
            continue  # 越界样例不填充，交给核验逻辑报错
        seed = sum(guid_to_bytes(unique_guid)) & 0xFF
        for lba in range(first, last + 1):
            offset = lba * SECTOR_SIZE
            pattern = bytes(
                (byte_index * 13 + lba * 7 + seed) & 0xFF
                for byte_index in range(SECTOR_SIZE)
            )
            image[offset : offset + SECTOR_SIZE] = pattern
        marker = f"RESCUE-DEMO PARTITION {name}\n".encode("utf-8")
        start = first * SECTOR_SIZE
        image[start : start + min(len(marker), 64)] = marker[:64]


def build_gpt_image(
    total_sectors: int,
    primary_specs: Sequence[PartitionSpec],
    backup_specs: Sequence[PartitionSpec],
    *,
    fill_content: bool = True,
) -> bytes:
    """按标准几何构造完整 GPT 盘镜像（主备表各自带正确 CRC）。

    主表固定在 LBA 1 / 数组 LBA 2..33；备表固定在盘尾：
    表头 LBA total-1，数组 LBA total-33..total-2。
    """
    image = bytearray(total_sectors * SECTOR_SIZE)

    # 保护性 MBR（LBA 0）：分区类型 0xEE + 55AA 签名。
    image[510] = 0x55
    image[511] = 0xAA
    image[446 + 4] = 0xEE
    struct.pack_into("<I", image, 446 + 8, 1)
    struct.pack_into("<I", image, 446 + 12, min(total_sectors - 1, 0xFFFFFFFF))

    if fill_content:
        # 同一分区可能同时出现在主备 specs 中；GUID 相同 => 图案相同。
        _fill_partition_content(image, list(primary_specs) + list(backup_specs),
                                total_sectors)

    primary_array = build_entry_array(primary_specs)
    backup_array = build_entry_array(backup_specs)
    import zlib

    primary_crc = zlib.crc32(primary_array) & 0xFFFFFFFF
    backup_crc = zlib.crc32(backup_array) & 0xFFFFFFFF

    primary_entry_lba = 2
    backup_header_lba = total_sectors - 1
    backup_entry_lba = total_sectors - 33

    image[
        primary_entry_lba * SECTOR_SIZE :
        primary_entry_lba * SECTOR_SIZE + ENTRY_ARRAY_BYTES
    ] = primary_array
    image[
        backup_entry_lba * SECTOR_SIZE :
        backup_entry_lba * SECTOR_SIZE + ENTRY_ARRAY_BYTES
    ] = backup_array

    primary_header = build_header_sector(
        header_lba=1,
        alternate_lba=backup_header_lba,
        entry_lba=primary_entry_lba,
        entries_crc=primary_crc,
        total_sectors=total_sectors,
    )
    backup_header = build_header_sector(
        header_lba=backup_header_lba,
        alternate_lba=1,
        entry_lba=backup_entry_lba,
        entries_crc=backup_crc,
        total_sectors=total_sectors,
    )
    image[1 * SECTOR_SIZE : 2 * SECTOR_SIZE] = primary_header
    image[
        backup_header_lba * SECTOR_SIZE :
        (backup_header_lba + 1) * SECTOR_SIZE
    ] = backup_header
    return bytes(image)


def build_demo_image() -> bytes:
    """网页/脚本使用的分歧主备表演示盘。"""
    return build_gpt_image(
        DEMO_SECTOR_COUNT, PRIMARY_SPECS, BACKUP_SPECS
    )
