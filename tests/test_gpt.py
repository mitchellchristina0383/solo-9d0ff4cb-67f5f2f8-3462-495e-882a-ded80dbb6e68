"""GPT 分区提取测试。

覆盖：
- 纯解析/核验：GUID、双 CRC、仅凭备表、主备冲突、双表损坏、缺扇区、
  边界越界与重叠；
- HTTP 接口：/api/gpt 决策、下载前复查（job_id / GUID / 范围）、
  缺扇区 409 带编号、收齐后原始字节一致、演示 setup/read 接口。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest

from rescue.app import create_app
from rescue.demo import (
    BACKUP_SPECS,
    DEMO_BAD_SECTORS,
    DEMO_FIRST_ROUND_SKIP,
    DEMO_SECTOR_COUNT,
    build_demo_image,
    build_gpt_image,
)
from rescue.gpt import (
    format_guid,
    guid_to_bytes,
    analyze_disk,
)
from rescue.storage import SECTOR_SIZE

TOTAL = 128
FIRST_USABLE = 34
LAST_USABLE = TOTAL - 34  # 94

TYPE_A = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
TYPE_B = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
GUID_A = "AAAAAAAA-1111-4111-8111-111111111111"
GUID_B = "BBBBBBBB-2222-4222-8222-222222222222"
GUID_C = "CCCCCCCC-3333-4333-8333-333333333333"

SPECS = [
    (0, TYPE_A, GUID_A, 40, 63, "efi"),
    (1, TYPE_B, GUID_B, 64, 90, "root"),
]


def memory_provider(image: bytes, saved=None):
    total = len(image) // SECTOR_SIZE
    if saved is None:
        saved_set = set(range(total))
    else:
        saved_set = set(saved)

    def read(lba):
        if 0 <= lba < total and lba in saved_set:
            return image[lba * SECTOR_SIZE : (lba + 1) * SECTOR_SIZE]
        return None

    missing = set(range(total)) - saved_set
    return read, missing


class GuidTests(unittest.TestCase):
    def test_known_efi_guid_bytes(self):
        # UEFI 规范里 EFI System Partition GUID 的标准字节序。
        raw = bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b")
        self.assertEqual(
            format_guid(raw), "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
        )

    def test_roundtrip(self):
        text = "12345678-9ABC-DEF0-1234-56789ABCDEF0"
        self.assertEqual(format_guid(guid_to_bytes(text)), text)


class GptParserTests(unittest.TestCase):
    def test_consistent_tables_use_primary(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        read, missing = memory_provider(image)
        res = analyze_disk(TOTAL, read, missing)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["source"], "primary")
        self.assertEqual([p["index"] for p in res["partitions"]], [0, 1])
        p0 = res["partitions"][0]
        self.assertEqual(p0["unique_guid"], GUID_A)
        self.assertEqual(p0["type_guid"], TYPE_A)
        self.assertEqual(p0["first_lba"], 40)
        self.assertEqual(p0["last_lba"], 63)
        self.assertEqual(p0["sector_count"], 24)
        self.assertTrue(p0["complete"])
        self.assertEqual(p0["missing_sectors"], [])

    def test_missing_primary_uses_backup(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        # 主 GPT 表头 + 数组扇区 1..33 全部未救出。
        saved = [i for i in range(TOTAL) if i not in range(1, 34)]
        read, missing = memory_provider(image, saved)
        res = analyze_disk(TOTAL, read, missing)
        self.assertEqual(res["primary"]["status"], "incomplete")
        self.assertIn(1, res["primary"]["missing_sectors"])
        self.assertEqual(res["backup"]["status"], "valid")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["source"], "backup")
        self.assertEqual(res["partitions"][1]["unique_guid"], GUID_B)

    def test_primary_header_crc_bad_falls_back_to_backup(self):
        raw = bytearray(build_gpt_image(TOTAL, SPECS, SPECS))
        raw[1 * SECTOR_SIZE + 40] ^= 0xFF  # 改 first_usable，表头 CRC 失配
        read, missing = memory_provider(bytes(raw))
        res = analyze_disk(TOTAL, read, missing)
        self.assertFalse(res["primary"]["header_crc_ok"])
        self.assertEqual(res["primary"]["status"], "invalid")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["source"], "backup")

    def test_primary_entry_array_crc_bad_falls_back(self):
        raw = bytearray(build_gpt_image(TOTAL, SPECS, SPECS))
        # 改分区项 0 的起始 LBA 低字节；表头里的数组 CRC 不重算。
        raw[2 * SECTOR_SIZE + 32] ^= 0x01
        read, missing = memory_provider(bytes(raw))
        res = analyze_disk(TOTAL, read, missing)
        self.assertTrue(res["primary"]["header_crc_ok"])
        self.assertFalse(res["primary"]["entries_crc_ok"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["source"], "backup")

    def test_conflicting_valid_tables_stop(self):
        primary = SPECS
        backup = SPECS + [(2, TYPE_B, GUID_C, 91, LAST_USABLE, "data")]
        image = build_gpt_image(TOTAL, primary, backup)
        read, missing = memory_provider(image)
        res = analyze_disk(TOTAL, read, missing)
        self.assertEqual(res["primary"]["status"], "valid")
        self.assertEqual(res["backup"]["status"], "valid")
        self.assertEqual(res["status"], "conflict")
        self.assertEqual(res["partitions"], [])
        joined = "\n".join(res["differences"])
        self.assertIn("分区项 3", joined)

    def test_demo_image_is_backup_then_conflict(self):
        image = build_demo_image()

        # 首轮：主 GPT 缺失 -> 仅凭备表。
        saved = [
            i for i in range(DEMO_SECTOR_COUNT)
            if i not in DEMO_FIRST_ROUND_SKIP
        ]
        read, missing = memory_provider(image, saved)
        first = analyze_disk(DEMO_SECTOR_COUNT, read, missing)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["source"], "backup")
        roots = next(p for p in first["partitions"] if p["index"] == 1)
        self.assertEqual(roots["missing_sectors"], [250, 400])

        # 补读主 GPT（全盘齐）-> 合法但冲突。
        read_all, missing_all = memory_provider(image)
        second = analyze_disk(DEMO_SECTOR_COUNT, read_all, missing_all)
        self.assertEqual(second["status"], "conflict")
        self.assertTrue(second["differences"])

    def test_both_crc_bad_is_invalid(self):
        raw = bytearray(build_gpt_image(TOTAL, SPECS, SPECS))
        raw[1 * SECTOR_SIZE + 40] ^= 0xFF
        raw[(TOTAL - 1) * SECTOR_SIZE + 40] ^= 0xFF
        read, missing = memory_provider(bytes(raw))
        res = analyze_disk(TOTAL, read, missing)
        self.assertEqual(res["primary"]["status"], "invalid")
        self.assertEqual(res["backup"]["status"], "invalid")
        self.assertEqual(res["status"], "invalid")
        self.assertEqual(res["partitions"], [])
        self.assertTrue(res["reasons"])

    def test_nothing_saved_is_incomplete_with_missing_numbers(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        read, missing = memory_provider(image, saved=[])
        res = analyze_disk(TOTAL, read, missing)
        self.assertEqual(res["status"], "incomplete")
        self.assertEqual(res["source"], None)
        self.assertIn(1, res["primary"]["missing_sectors"])
        self.assertIn(TOTAL - 1, res["backup"]["missing_sectors"])

    def test_partition_beyond_disk_rejected(self):
        bad_specs = [(0, TYPE_A, GUID_A, 40, TOTAL + 50, "bad")]
        image = build_gpt_image(TOTAL, bad_specs, bad_specs)
        read, missing = memory_provider(image)
        res = analyze_disk(TOTAL, read, missing)
        self.assertEqual(res["status"], "invalid")
        reasons = " ".join(
            res["primary"]["reasons"] + res["backup"]["reasons"]
        )
        self.assertIn("超出盘尾", reasons)

    def test_overlapping_partitions_rejected(self):
        overlap = [
            (0, TYPE_A, GUID_A, 40, 70, "a"),
            (1, TYPE_B, GUID_B, 60, 90, "b"),
        ]
        image = build_gpt_image(TOTAL, overlap, overlap)
        read, missing = memory_provider(image)
        res = analyze_disk(TOTAL, read, missing)
        self.assertEqual(res["status"], "invalid")
        reasons = " ".join(res["primary"]["reasons"])
        self.assertIn("重叠", reasons)

    def test_missing_sectors_annotated_per_partition(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        saved = set(range(TOTAL)) - {45, 80, 81}
        read, missing = memory_provider(image, saved)
        res = analyze_disk(TOTAL, read, missing)
        p0 = next(p for p in res["partitions"] if p["index"] == 0)
        p1 = next(p for p in res["partitions"] if p["index"] == 1)
        self.assertEqual(p0["missing_sectors"], [45])
        self.assertFalse(p0["complete"])
        self.assertEqual(p1["missing_sectors"], [80, 81])


class GptApiTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rescue-gpt-api-")
        self.app = create_app(self.tmp)
        self.app.testing = True
        self.client = self.app.test_client()
        self.storage = self.app.extensions["rescue_storage"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_job(self, image: bytes, skip=()):
        resp = self.client.post(
            "/api/job",
            data={"image": (io.BytesIO(image), "sample.img")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        total = len(image) // SECTOR_SIZE
        skip_set = set(skip)
        items = [
            (i, image[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE])
            for i in range(total)
            if i not in skip_set
        ]
        if items:
            self.storage.save_batch(items)
        return resp.get_json()["progress"]["job_id"]

    def gpt(self):
        resp = self.client.get("/api/gpt")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def download(self, slot, job_id, guid, start, end, type_guid=TYPE_A):
        q = (
            f"?job_id={job_id}&guid={guid}&type_guid={type_guid}"
            f"&start={start}&end={end}"
        )
        return self.client.get(f"/api/gpt/partitions/{slot}/raw{q}")


class GptApiBehaviorTests(GptApiTestBase):
    def test_gpt_without_job_404(self):
        self.assertEqual(self.client.get("/api/gpt").status_code, 404)

    def test_get_gpt_and_job_id(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        job_id = self.start_job(image)
        body = self.gpt()
        self.assertTrue(body["ok"])
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(len(body["partitions"]), 2)

    def test_download_complete_partition_bytes(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        job_id = self.start_job(image)
        resp = self.download(0, job_id, GUID_A, 40, 63)
        self.assertEqual(resp.status_code, 200)
        expected = image[40 * 512 : 64 * 512]
        self.assertEqual(resp.data, expected)
        self.assertEqual(resp.headers["X-Partition-GUID"], GUID_A)
        self.assertIn("attachment", resp.headers["Content-Disposition"])

    def test_download_without_job_id_rejected(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        self.start_job(image)
        resp = self.client.get(
            f"/api/gpt/partitions/0/raw?guid={GUID_A}&start=40&end=63"
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "job_changed")

    def test_wrong_job_id_prompts_refresh(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        self.start_job(image)
        resp = self.download(0, "stale-job-id", GUID_A, 40, 63)
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertEqual(body["code"], "job_changed")
        self.assertIn("刷新", body["error"])

    def test_task_replaced_old_job_id_conflict(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        old_job = self.start_job(image)
        self.assertEqual(self.client.delete("/api/job").status_code, 200)

        other = bytes((i * 17 + 3) % 256 for i in range(10 * 512))
        self.start_job(other)  # 新任务 job_id 不同
        resp = self.download(0, old_job, GUID_A, 40, 63)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "job_changed")

    def test_guid_or_range_change_prompts_refresh(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        job_id = self.start_job(image)

        bad_end = self.download(0, job_id, GUID_A, 40, 62)
        self.assertEqual(bad_end.status_code, 409)
        self.assertEqual(bad_end.get_json()["code"], "partition_changed")

        bad_guid = self.download(0, job_id, "00000000-0000-4000-8000-000000000000",
                                 40, 63)
        self.assertEqual(bad_guid.status_code, 409)
        self.assertEqual(bad_guid.get_json()["code"], "partition_changed")

    def test_incomplete_partition_lists_missing_then_downloads(self):
        image = build_gpt_image(TOTAL, SPECS, SPECS)
        job_id = self.start_job(image, skip={50, 70})

        body = self.gpt()
        p0 = next(p for p in body["partitions"] if p["index"] == 0)
        self.assertEqual(p0["missing_sectors"], [50])

        blocked = self.download(0, job_id, GUID_A, 40, 63)
        self.assertEqual(blocked.status_code, 409)
        payload = blocked.get_json()
        self.assertEqual(payload["code"], "partition_incomplete")
        self.assertEqual(payload["missing"], [50])

        # 按编号补读（演示接口从样盘取数）。
        r = self.client.post(
            "/api/demo/read",
            data=json.dumps({"sectors": [50]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["saved_now"], [50])

        ok = self.download(0, job_id, GUID_A, 40, 63)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data, image[40 * 512 : 64 * 512])

        # 另一个分区仍缺 70，不影响分区 1 已收齐的结论。
        blocked_b = self.download(1, job_id, GUID_B, 64, 90, type_guid=TYPE_B)
        self.assertEqual(blocked_b.status_code, 409)
        self.assertEqual(blocked_b.get_json()["missing"], [70])

    def test_conflict_blocks_download(self):
        backup_specs = SPECS + [(2, TYPE_B, GUID_C, 91, LAST_USABLE, "data")]
        image = build_gpt_image(TOTAL, SPECS, backup_specs)
        job_id = self.start_job(image)

        body = self.gpt()
        self.assertEqual(body["status"], "conflict")

        resp = self.download(0, job_id, GUID_A, 40, 63)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "gpt_conflict")


class DemoApiTests(GptApiTestBase):
    def test_demo_sample_endpoint(self):
        resp = self.client.get("/api/demo/sample.img")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, build_demo_image())
        self.assertIn("sample-gpt-demo.img",
                      resp.headers["Content-Disposition"])

    def test_demo_setup_backup_extraction_then_conflict_after_reread(self):
        resp = self.client.post("/api/demo/setup")
        self.assertEqual(resp.status_code, 201)
        job_id = resp.get_json()["progress"]["job_id"]

        body = self.gpt()
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["source"], "backup")
        root = next(p for p in body["partitions"] if p["index"] == 1)
        self.assertEqual(root["missing_sectors"], [250, 400])
        efi = next(p for p in body["partitions"] if p["index"] == 0)
        self.assertEqual(efi["missing_sectors"], [DEMO_BAD_SECTORS[0]])

        # 重复 setup 与普通上传一样 409。
        self.assertEqual(self.client.post("/api/demo/setup").status_code, 409)

        # 补读全部缺失 -> 主 GPT 也被补回 -> 冲突。
        r = self.client.post(
            "/api/demo/read",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn(1, r.get_json()["saved_now"])
        body2 = self.gpt()
        self.assertEqual(body2["status"], "conflict")

        # 冲突后旧的分区下载请求被要求刷新。
        dl = self.download(1, job_id, root["unique_guid"],
                           root["first_lba"], root["last_lba"],
                           type_guid=root["type_guid"])
        self.assertEqual(dl.status_code, 409)
        self.assertEqual(dl.get_json()["code"], "gpt_conflict")

    def test_demo_read_without_job_404(self):
        resp = self.client.post("/api/demo/read", json={"sectors": [1]})
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
