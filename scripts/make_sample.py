#!/usr/bin/env python3
"""把代码内构造的 GPT 演示样盘写成文件，供 sim_reader / 第三方工具使用。

用法：
  python3 scripts/make_sample.py [samples/sample-gpt.img]

样盘特性见 rescue/demo.py 模块说明。脚本末尾会打印配合 sim_reader
演练“仅凭备表提取 -> 补读主表 -> 主备冲突”的 --fail 参数。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rescue.demo import (  # noqa: E402
    DEMO_BAD_SECTORS,
    DEMO_FIRST_ROUND_SKIP,
    DEMO_PRIMARY_GPT_SECTORS,
    DEMO_SECTOR_COUNT,
    build_demo_image,
)


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "samples", "sample-gpt.img"
    )
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    image = build_demo_image()
    with open(out, "wb") as fh:
        fh.write(image)

    fail_all = ",".join(str(i) for i in DEMO_FIRST_ROUND_SKIP)
    fail_primary = ",".join(str(i) for i in DEMO_PRIMARY_GPT_SECTORS)
    fail_bad = ",".join(str(i) for i in DEMO_BAD_SECTORS)
    print(f"已写出 {out}：{DEMO_SECTOR_COUNT} 扇区 / {len(image)} 字节")
    print()
    print("第 1 轮（主 GPT + 分区内坏扇区都读不出，仅凭备表提取）：")
    print(f"  python3 scripts/sim_reader.py --image {out} --fail {fail_all}")
    print()
    print("第 2 轮：只补分区内坏扇区（继续跳过 1..33，备表可正常下载分区）：")
    print(f"  python3 scripts/sim_reader.py --image {out} --fail {fail_primary}")
    print()
    print("第 3 轮：不传 --fail，把主 GPT 也补读回来 -> 主备布局冲突、停止提取：")
    print(f"  python3 scripts/sim_reader.py --image {out} --rounds 1")
    print(f"  # 分区内坏扇区编号：{fail_bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
