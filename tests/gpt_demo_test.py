#!/usr/bin/env python3
"""GPT 提取场景自测（真实起 HTTP 服务）。

严格对应题目要求人工自测的两条主线：
  1. 仅凭备 GPT 提取：首轮主 GPT 未救出 -> 采用备表；分区缺扇区时列出
     编号；补读收齐后下载到的分区原始字节与样盘逐字节一致。
  2. 补读后的主备冲突：把主 GPT 补读回来 -> 两份都合法但布局不同 ->
     /api/gpt 报 conflict，旧的分区下载请求被 409 gpt_conflict 挡住。

另验证：
  - 下载/聚合分区期间，其他扇区的补读上报可以并发完成（无全局阻塞/死锁）；
  - 任务重置更换后，旧 job_id 的下载请求收到 job_changed（提示刷新）。

运行：python3 -I tests/gpt_demo_test.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))

from rescue.demo import build_demo_image  # noqa: E402
from tests.integration_test import Client, check, start_server  # noqa: E402

PORT = 8931


def get_error_json(url: str):
    try:
        urllib.request.urlopen(url, timeout=30)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())
    raise AssertionError(f"{url} 应当返回 4xx/5xx")


def raw_url(client: Client, path_with_query: str) -> str:
    return client.base + path_with_query


def main() -> int:
    image = build_demo_image()
    work = __import__("tempfile").mkdtemp(prefix="rescue-gpt-selftest-")
    proc = start_server(work, PORT)
    client = Client(f"http://127.0.0.1:{PORT}")
    try:
        # 页面包含 GPT 提取入口 -------------------------------------- #
        html = urllib.request.urlopen(client.base + "/", timeout=10).read().decode()
        check("GPT 分区提取" in html and "/api/gpt" in html,
              "救援页包含 GPT 分区提取面板与 /api/gpt 逻辑")

        # 1) 一键演示 setup ------------------------------------------ #
        setup = client.post_json("/api/demo/setup", {})
        job_id = setup["progress"]["job_id"]
        check(setup["progress"]["saved_count"] > 900,
              f"演示首轮已模拟读盘 {setup['progress']['saved_count']} 个扇区")

        gpt = client.get_json("/api/gpt")
        check(gpt["status"] == "ok" and gpt["source"] == "backup",
              "主 GPT 缺失时，仅凭有效备表完成分析")
        check(gpt["primary"]["status"] == "incomplete",
              "主 GPT 状态为 incomplete 并给出缺失扇区")
        check(gpt["backup"]["status"] == "valid", "备 GPT 双 CRC 与边界核验通过")

        by_slot = {p["index"]: p for p in gpt["partitions"]}
        root = by_slot[1]
        data_p = by_slot[2]
        check(root["missing_sectors"] == [250, 400],
              f"root 分区缺失编号恰为 [250,400]，实际 {root['missing_sectors']}")
        check(data_p["missing_sectors"] == [700, 900],
              f"data 分区缺失编号恰为 [700,900]，实际 {data_p['missing_sectors']}")
        print("  分区清单：")
        for p in gpt["partitions"]:
            print(f"    #{p['index'] + 1} {p['name']:<12} {p['unique_guid']} "
                  f"LBA {p['first_lba']}..{p['last_lba']} "
                  f"缺 {p['missing_sectors']}")

        def part_query(p, jid=job_id):
            return (f"/api/gpt/partitions/{p['index']}/raw"
                    f"?job_id={jid}&guid={p['unique_guid']}"
                    f"&type_guid={p['type_guid']}"
                    f"&start={p['first_lba']}&end={p['last_lba']}")

        # 2) 未收齐 -> 409 + 编号 ------------------------------------- #
        code, body = get_error_json(raw_url(client, part_query(root)))
        check(code == 409 and body["code"] == "partition_incomplete",
              "缺扇区时点下载被 409 partition_incomplete 挡住")
        check(body["missing"] == [250, 400],
              "409 响应列出该分区缺失扇区编号 [250,400]")

        # 3) 补读收齐 -> 原始字节与样盘一致 --------------------------- #
        r = client.post_json("/api/demo/read", {"sectors": [250, 400]})
        check(r["saved_now"] == [250, 400], "按编号补读 250/400 成功")
        _h, blob = client.get_bytes(part_query(root))
        expect = image[root["first_lba"] * 512:(root["last_lba"] + 1) * 512]
        check(len(blob) == root["sector_count"] * 512,
              f"下载长度为分区原始长度 {len(expect)} 字节（无偏移填充）")
        check(blob == expect, "root 分区原始字节与演示样盘对应区间逐字节一致")

        # 4) 下载期间并发补读其他扇区 --------------------------------- #
        # 先把 data 分区补收齐，提供较大的下载体。
        client.post_json("/api/demo/read", {"sectors": [700, 900, 100]})
        gpt2 = client.get_json("/api/gpt")
        data_p2 = {p["index"]: p for p in gpt2["partitions"]}[2]
        check(data_p2["complete"], "data 分区补读后收齐")

        download_url = raw_url(client, part_query(data_p2))
        expect_data = image[
            data_p2["first_lba"] * 512:(data_p2["last_lba"] + 1) * 512
        ]

        def downloads(_round):
            with urllib.request.urlopen(download_url, timeout=30) as resp:
                content = resp.read()
            assert content == expect_data, "并发下载的数据不一致"
            return len(content)

        def reread_writer(seed):
            # 反复补读其他（已保存的）扇区，模拟下载期间读盘不中断。
            sector = 64 + (seed % 10)  # EFI 分区内已救出的扇区
            resp = client.post_json("/api/demo/read", {"sectors": [sector]})
            assert resp["ok"]

        with ThreadPoolExecutor(max_workers=12) as pool:
            dl_futs = [pool.submit(downloads, i) for i in range(24)]
            wr_futs = [
                pool.submit(reread_writer, i)
                for _ in range(6) for i in range(10)
            ]
            sizes = [f.result(timeout=60) for f in dl_futs]
            for f in wr_futs:
                f.result(timeout=60)
        check(len(sizes) == 24 and all(s == len(expect_data) for s in sizes),
              "24 个并发分区下载全部得到完整正确字节")
        check(True, "60 个下载期间的补读/重复上报请求全部成功，无阻塞死锁")

        # 5) 补读主 GPT -> 主备冲突 ---------------------------------- #
        primary_missing = gpt["primary"]["missing_sectors"]
        check(primary_missing == list(range(1, 34)),
              "主 GPT 缺失清单恰为 1..33")
        client.post_json("/api/demo/read", {"sectors": primary_missing})

        gpt3 = client.get_json("/api/gpt")
        check(gpt3["status"] == "conflict",
              "补读主 GPT 后：两份均合法但布局不同，状态 conflict")
        check(len(gpt3["differences"]) >= 2,
              f"给出 {len(gpt3['differences'])} 条具体布局差异说明")
        for d in gpt3["differences"]:
            print("    差异：", d)

        code, body = get_error_json(raw_url(client, part_query(root)))
        check(code == 409 and body["code"] == "gpt_conflict",
              "冲突后下载旧分区被 409 gpt_conflict 挡住并要求刷新")

        # 6) 任务更换 -> job_changed ---------------------------------- #
        old_job = job_id
        client.delete("/api/job")
        client.post_json("/api/demo/setup", {})
        code, body = get_error_json(raw_url(client, part_query(root, old_job)))
        check(code == 409 and body["code"] == "job_changed",
              "任务重置更换后，旧 job_id 下载收到 job_changed（提示刷新）")

        print("\nGPT 场景自测全部通过 ✅")
        return 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print("HTTP 错误", exc.code, exc.read().decode(errors="replace"))
        raise
