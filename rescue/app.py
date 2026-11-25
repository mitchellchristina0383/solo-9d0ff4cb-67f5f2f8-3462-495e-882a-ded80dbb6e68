"""Flask 救援页：上传样盘、上报读出扇区、下载同进度镜像与缺失清单。"""

from __future__ import annotations

import base64
import io
import json
import os
from typing import Tuple

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
)
from werkzeug.utils import secure_filename

from .demo import (
    DEMO_BAD_SECTORS,
    DEMO_FIRST_ROUND_SKIP,
    DEMO_PRIMARY_GPT_SECTORS,
    build_demo_image,
)
from .gpt import analyze_disk
from .storage import (
    MAX_IMAGE_SIZE,
    SECTOR_SIZE,
    JobExistsError,
    JobStorage,
    StorageError,
)


def create_app(data_dir: str | None = None) -> Flask:
    template_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "templates")
    )
    app = Flask(__name__, template_folder=template_dir)
    data_dir = data_dir or os.environ.get("RESCUE_DATA_DIR", "/data")
    app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_SIZE + 1 * 1024 * 1024
    storage = JobStorage(data_dir)
    app.extensions["rescue_storage"] = storage

    # ---------------------------------------------------------------- #
    # 页面
    # ---------------------------------------------------------------- #
    @app.get("/")
    def index():
        return render_template(
            "index.html",
            max_mib=MAX_IMAGE_SIZE // (1024 * 1024),
            sector_size=SECTOR_SIZE,
        )

    # ---------------------------------------------------------------- #
    # 任务
    # ---------------------------------------------------------------- #
    @app.post("/api/job")
    def upload_sample():
        upload = request.files.get("image")
        if upload is None:
            return _error(400, "缺少 image 上传字段")
        filename = secure_filename(upload.filename or "sample.img")
        if not filename:
            return _error(400, "文件名无效")
        try:
            state = storage.create_job(upload.stream)
        except JobExistsError as exc:
            return _error(409, str(exc))
        except StorageError as exc:
            return _error(400, str(exc))
        return jsonify({"ok": True, "progress": state}), 201

    @app.get("/api/job")
    def job_status():
        if not storage.has_job():
            return _error(404, "尚无救援任务，请先上传样盘")
        return jsonify({"ok": True, "progress": storage.get_state()})

    @app.get("/api/missing")
    def current_missing():
        """当前最新缺失清单（便捷接口；强一致请走 /api/snapshots）。"""
        if not storage.has_job():
            return _error(404, "尚无救援任务")
        state = storage.get_state()
        return jsonify(
            {
                "ok": True,
                "sector_count": state["sector_count"],
                "missing_count": state["missing_count"],
                "missing": state["missing"],
            }
        )

    @app.delete("/api/job")
    def reset_job():
        storage.reset()
        return jsonify({"ok": True})

    # ---------------------------------------------------------------- #
    # 读盘上报
    # ---------------------------------------------------------------- #
    @app.post("/api/sectors")
    def report_sectors():
        """恢复工具上报本轮读出的扇区。

        支持两种载荷：
        1. JSON：{"sectors": [{"sector": 12, "data": "<base64>"}]}
        2. 单扇区二进制：query 参数 ?sector=N，body 恰好 512 字节
        已保存的扇区重复上报幂等返回 already_saved；失败的扇区不上报
        即保持缺失，下一轮继续补读。
        """
        if not storage.has_job():
            return _error(404, "尚无救援任务")

        if request.content_type and request.content_type.startswith("application/json"):
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("sectors"), list
            ):
                return _error(400, "JSON 载荷需为 {\"sectors\": [{\"sector\", \"data\"}]}")
            items = []
            parse_errors = []
            for entry in payload["sectors"]:
                parsed = _parse_json_sector(entry)
                if isinstance(parsed, tuple):
                    items.append(parsed)
                else:
                    # 单条解析失败不拖垮整批，记录后继续处理其他条目
                    bad_sector = entry.get("sector") if isinstance(entry, dict) else "?"
                    parse_errors.append({"sector": bad_sector, "error": parsed})
        else:
            sector_raw = request.args.get("sector", "")
            try:
                index = int(sector_raw)
            except ValueError:
                return _error(400, "query 参数 sector 必须为非负整数")
            data = request.get_data(cache=False)
            if len(data) != SECTOR_SIZE:
                return _error(
                    400,
                    f"单扇区二进制上报长度必须为 {SECTOR_SIZE} 字节，实际 {len(data)}",
                )
            items, parse_errors = [(index, data)], []

        result = storage.save_batch(items)
        result["rejected"] = parse_errors + result["rejected"]
        return jsonify({"ok": True, **result})

    @app.get("/api/sectors/<int:index>")
    def get_sector(index: int):
        """取回某个已救出的原始扇区；缺失返回 409。"""
        try:
            data = storage.read_saved_sector(index)
        except StorageError as exc:
            return _error(400, str(exc))
        if data is None:
            return _error(409, f"扇区 {index} 仍缺失")
        return Response(data, mimetype="application/octet-stream")

    # ---------------------------------------------------------------- #
    # GPT 分区提取（不必等整盘救完）
    # ---------------------------------------------------------------- #
    def _gpt_read_sector(index: int):
        """给 gpt.analyze_disk 用的只读回调；越界按“读不到”处理。"""
        try:
            return storage.read_saved_sector(index)
        except StorageError:
            return None

    def _current_gpt():
        """基于当前救援进度做一次 GPT 分析，返回 (state, analysis)。"""
        state = storage.get_state()
        analysis = analyze_disk(
            state["sector_count"],
            _gpt_read_sector,
            missing_set=set(state["missing"]),
        )
        return state, analysis

    @app.get("/api/gpt")
    def gpt_status():
        """用已救出的扇区核验主/备 GPT，给出分区列表或停止提取的原因。"""
        if not storage.has_job():
            return _error(404, "尚无救援任务，请先上传样盘")
        state, analysis = _current_gpt()
        return jsonify({"ok": True, "job_id": state["job_id"], **analysis})

    @app.get("/api/gpt/partitions/<int:slot>/raw")
    def download_partition(slot: int):
        """下载单个分区的原始字节。

        点击下载时在**同一个当前进度**上依次复查：
        1. job_id —— 任务是否已被重置/更换；
        2. 主备 GPT 决策 —— 是否仍有唯一可用且无冲突的表；
        3. 所选槽位分区的唯一 GUID、类型 GUID 与起止 LBA 是否变化；
        4. 分区范围内是否还有缺失扇区（有则 409 + 缺失编号）。
        全部通过才输出该分区的原始字节（不含任何偏移填充）。
        """
        if not storage.has_job():
            return _error(404, "尚无救援任务")

        job_id = (request.args.get("job_id") or "").strip()
        state = storage.get_state()
        if not job_id or job_id != state.get("job_id"):
            return _conflict(
                "job_changed",
                "任务已更换或重置，页面上的分区信息来自旧任务，请刷新后重试",
            )

        guid = (request.args.get("guid") or "").strip().upper()
        type_guid = (request.args.get("type_guid") or "").strip().upper()
        try:
            start = int(request.args.get("start", ""))
            end = int(request.args.get("end", ""))
        except (TypeError, ValueError):
            return _error(400, "start/end 必须是整数扇区号")
        if not guid:
            return _error(400, "缺少 guid（分区唯一 GUID）参数")

        _state, analysis = _current_gpt()
        if analysis["status"] != "ok":
            code = {
                "conflict": "gpt_conflict",
                "invalid": "gpt_invalid",
                "incomplete": "gpt_incomplete",
            }.get(analysis["status"], "gpt_unavailable")
            return _conflict(
                code,
                "当前进度下 GPT 已不能支持提取，请按最新分析刷新后再选分区下载",
                gpt=analysis,
            )

        part = next(
            (p for p in analysis["partitions"] if p["index"] == slot), None
        )
        if part is None:
            return _conflict(
                "partition_changed",
                f"分区项 {slot + 1} 在最新分区表中已不存在，请刷新分区列表",
                gpt=analysis,
            )

        identity_changed = (
            part["unique_guid"].upper() != guid
            or part["first_lba"] != start
            or part["last_lba"] != end
            or (type_guid and part["type_guid"].upper() != type_guid)
        )
        if identity_changed:
            return _conflict(
                "partition_changed",
                "所选分区的 GUID 或起止范围已随救援进度变化，请刷新后重新选择",
                gpt=analysis,
            )

        # 以存储层锁内的当前 saved 集合做权威缺失检查。
        try:
            missing = storage.missing_in_range(
                part["first_lba"], part["last_lba"]
            )
        except StorageError as exc:
            return _error(400, str(exc))
        if missing:
            return _conflict(
                "partition_incomplete",
                f"分区仍缺 {len(missing)} 个扇区，补读收齐后才能下载原始字节",
                missing=missing,
            )

        # 逐扇区短锁读取并在内存拼出整个分区（全盘上限 8 MiB）。
        # 不长期持有存储锁、不跨网络发送持锁，故下载期间其他扇区照常上报。
        chunks = []
        for lba in range(part["first_lba"], part["last_lba"] + 1):
            data = storage.read_saved_sector(lba)
            if data is None:
                # 仅当读取途中任务被 reset 才可能发生（扇区只会新增）。
                return _conflict(
                    "job_changed",
                    "读取分区字节时任务被重置，请刷新后重试",
                )
            chunks.append(data)
        blob = b"".join(chunks)

        filename = (
            f"partition-{slot + 1}-{part['first_lba']}-{part['last_lba']}.bin"
        )
        resp = Response(blob, mimetype="application/octet-stream")
        resp.headers["Content-Length"] = str(len(blob))
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{filename}"'
        )
        resp.headers["X-Job-Id"] = state["job_id"]
        resp.headers["X-Partition-GUID"] = part["unique_guid"]
        return resp

    # ---------------------------------------------------------------- #
    # 网页演示：内置 GPT 样盘 + 模拟恢复工具补读
    # ---------------------------------------------------------------- #
    @app.get("/api/demo/sample.img")
    def demo_sample_image():
        """下载代码内构造的 GPT 演示样盘（也可供 sim_reader 使用）。"""
        image = build_demo_image()
        resp = Response(image, mimetype="application/octet-stream")
        resp.headers["Content-Length"] = str(len(image))
        resp.headers["Content-Disposition"] = (
            'attachment; filename="sample-gpt-demo.img"'
        )
        return resp

    @app.post("/api/demo/setup")
    def demo_setup():
        """一键建演示任务，并模拟“首轮读盘”：

        除主 GPT 扇区（1..33）和几个分区内坏扇区外，其余扇区全部上报。
        已有任务时与普通上传一样返回 409，请先重置。
        """
        image = build_demo_image()
        try:
            storage.create_job(io.BytesIO(image))
        except JobExistsError as exc:
            return _error(409, str(exc))

        total = len(image) // SECTOR_SIZE
        skipped = set(DEMO_FIRST_ROUND_SKIP)
        items = [
            (i, image[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE])
            for i in range(total)
            if i not in skipped
        ]
        result = storage.save_batch(items)
        return (
            jsonify(
                {
                    "ok": True,
                    "progress": result["progress"],
                    "demo": {
                        "first_round_skip": DEMO_FIRST_ROUND_SKIP,
                        "primary_gpt_sectors": DEMO_PRIMARY_GPT_SECTORS,
                        "bad_sectors": DEMO_BAD_SECTORS,
                    },
                }
            ),
            201,
        )

    @app.post("/api/demo/read")
    def demo_read_from_sample():
        """模拟恢复工具成功补读：从已上传样盘取指定扇区并按正常路径上报。

        JSON：{"sectors": [100, 250]}；省略 sectors 或为空时补读当前全部
        缺失扇区。真实作业中应改用 /api/sectors 上报故障盘实际读出的数据。
        """
        if not storage.has_job():
            return _error(404, "尚无救援任务")

        payload = request.get_json(silent=True)
        raw_sectors = payload.get("sectors") if isinstance(payload, dict) else None
        state = storage.get_state()
        if raw_sectors is None:
            indexes = list(state["missing"])
        else:
            if not isinstance(raw_sectors, list):
                return _error(400, "sectors 必须是扇区号数组")
            indexes = raw_sectors

        items = []
        rejected = []
        for raw in indexes:
            try:
                index = int(raw)
            except (TypeError, ValueError):
                rejected.append({"sector": raw, "error": "扇区号不是整数"})
                continue
            try:
                data = storage.read_sample_sector(index)
            except StorageError as exc:
                rejected.append({"sector": index, "error": str(exc)})
                continue
            items.append((index, data))

        result = storage.save_batch(items)
        result["rejected"] = rejected + result["rejected"]
        return jsonify({"ok": True, **result})

    # ---------------------------------------------------------------- #
    # 同进度快照下载
    # ---------------------------------------------------------------- #
    @app.post("/api/snapshots")
    def create_snapshot():
        try:
            snapshot = storage.create_snapshot()
        except StorageError as exc:
            return _error(404, str(exc))
        return jsonify({"ok": True, "snapshot": snapshot}), 201

    @app.get("/api/snapshots/<snapshot_id>/image.bin")
    def download_image(snapshot_id: str):
        generator = storage.open_snapshot_image(snapshot_id)
        if generator is None:
            abort(404, description="快照不存在或已过期")
        state = storage.get_state()
        resp = Response(generator, mimetype="application/octet-stream")
        resp.headers["Content-Length"] = str(
            state["sector_count"] * SECTOR_SIZE
        )
        resp.headers["X-Snapshot-Id"] = snapshot_id
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="rescue-{snapshot_id}.img"'
        )
        return resp

    @app.get("/api/snapshots/<snapshot_id>/missing.json")
    def download_missing(snapshot_id: str):
        payload = storage.snapshot_missing_list(snapshot_id)
        if payload is None:
            abort(404, description="快照不存在或已过期")
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        resp = Response(text, mimetype="application/json")
        resp.headers["X-Snapshot-Id"] = snapshot_id
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="missing-{snapshot_id}.json"'
        )
        return resp

    @app.errorhandler(413)
    def too_large(_err):
        return _error(413, f"请求体超过上限（样盘最大 {MAX_IMAGE_SIZE} 字节）")

    @app.errorhandler(404)
    def not_found(err):
        return _error(404, getattr(err, "description", "资源不存在"))

    return app


def _parse_json_sector(entry) -> Tuple[int, bytes] | str:
    if not isinstance(entry, dict) or "sector" not in entry or "data" not in entry:
        return "每个扇区需包含 sector 与 data 字段"
    try:
        index = int(entry["sector"])
    except (TypeError, ValueError):
        return f"扇区号非法：{entry.get('sector')!r}"
    try:
        data = base64.b64decode(entry["data"], validate=True)
    except (ValueError, TypeError):
        return f"扇区 {index} 的 data 不是合法 base64"
    return index, data


def _error(status: int, message: str):
    return jsonify({"ok": False, "error": message}), status


def _conflict(code: str, message: str, **extra):
    """409 状态冲突：前端按 code 决定提示刷新还是列出缺失扇区。"""
    body = {"ok": False, "code": code, "error": message}
    body.update(extra)
    return jsonify(body), 409
