# 坏盘救援台（Disk Rescue）

旧盘部分扇区读不出来时，值班员用本服务把恢复工具已读出的数据先存下来，
多轮补读直到整盘救回。Flask 实现，Docker Compose 一键启动。

## 功能与关键语义

- **样盘上传**：按 512 字节分扇区的原始镜像（raw image），大小必须是
  512 的整数倍，范围 1 扇区 ~ 8 MiB（16384 扇区），流式校验、超限即拒。
- **陆续读盘**：恢复工具每读出一个扇区就上报；读失败的扇区不上报即保持
  缺失，继续往后读。下一轮先拉缺失清单，只补读未救出的扇区。
- **中断安全**：每个扇区独立文件，写入走「临时文件 + fsync + 原子 rename
  + 目录 fsync」。重启时以磁盘扇区文件为准重建状态：已完整保存的扇区仍
  可用，写到一半的 `*.tmp` 在启动对账时删除，仍算缺失。
- **同进度下载**：先创建快照（snapshot），救援镜像与缺失清单共用同一个
  `snapshot_id`，保证两者对应同一份进度；下载期间读盘可以继续，快照内容
  不随后续读盘变化。镜像中缺失位置补零；**实际读到的全零扇区正常保存、
  不算缺失**（与补零位置可通过缺失清单区分）。
- **GPT 分区提取（不必等整盘救完）**：救援页第 5 区只用已救出的扇区核验
  主、备 GPT（标准 128 分区项 × 128 字节），分别校验表头 CRC32 与分区项
  数组 CRC32，并检查分区边界（起止合法、不越盘、不越过 usable 区间、互不
  重叠）。只有一份有效就采用该份；两份都有效但布局不同、或两份都无法通过
  核验（CRC/边界失败或扇区未齐）时说明原因并停止提取。页面列出每个可识别
  分区的类型/唯一 GUID、名称、起止 LBA、大小和范围内缺失扇区编号。
- **分区原始字节下载前复查**：点击下载时在当前最新进度上重新分析 GPT，并
  依次核对 `job_id`（任务是否已换）、表状态（是否仍无冲突）、所选槽位分区
  的 GUID 与起止范围；任务已换或 GUID/范围变化返回 `409` + 对应 code，
  前端提示刷新。分区仍缺扇区时 `409 partition_incomplete` 带回缺失编号，
  收齐后才输出该分区的原始字节（无偏移填充）。组装只走逐扇区短锁，不持有
  全局锁、不跨网络持锁，**下载期间其他扇区仍可照常补读上报**。
- **网页可演示样盘**：内置代码构造的 GPT 样盘（`rescue/demo.py`，1024 扇区），
  页面上“一键载入 GPT 演示样盘”会建任务并模拟首轮读盘（故意不救主 GPT
  LBA 1–33 和 5 个分区内坏扇区）；也可下载 `samples/sample-gpt.img` 配合
  `scripts/sim_reader.py` 走真实 HTTP 演练。
- 重复上报幂等；单条非法扇区不拖垮整批，错误在 `rejected` 中逐条返回。

## 目录结构

```
rescue/storage.py     持久化层：样盘、扇区原子写、崩溃对账、快照、job_id
rescue/gpt.py         纯函数 GPT 核验/解析/主备决策（不做磁盘 I/O）
rescue/demo.py        网页演示样盘构造（主备分歧 + 分区内坏扇区）
rescue/app.py         Flask 路由与 JSON 接口
templates/index.html  救援页（上传 / 进度 / 上报 / 快照 / GPT 提取）
wsgi.py               gunicorn 入口
scripts/sim_reader.py  模拟恢复工具的读盘脚本
scripts/make_sample.py 生成 samples/sample-gpt.img 并打印演练命令
tests/                单元测试、端到端集成测试、GPT 场景自测、冒烟测试
Dockerfile, docker-compose.yml
```

数据目录（容器内 `/data`，命名卷 `rescue-data`）：

```
/data
├── job.json            # 状态缓存（真相以 sectors/ 下的文件为准）
├── sample.img          # 样盘
└── sectors/<分片>/<扇区号>.bin
```

## 用 Docker Compose 启动

```bash
docker compose up --build -d
# 打开 http://localhost:8000
docker compose logs -f
docker compose down          # 停止；数据保留在命名卷 rescue-data
docker compose down -v       # 停止并删除救援数据
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 救援页 |
| POST | `/api/job` | multipart 上传 `image` 字段建任务 |
| GET | `/api/job` | 当前进度（saved / missing 列表、SHA-256） |
| GET | `/api/missing` | 当前缺失扇区清单（每轮补读前拉取） |
| DELETE | `/api/job` | 重置任务 |
| POST | `/api/sectors` | 上报读出扇区（JSON 批量或单扇区二进制） |
| GET | `/api/sectors/<N>` | 取回已救出的原始扇区（缺失返回 409） |
| POST | `/api/snapshots` | 生成进度快照，返回 `snapshot_id` |
| GET | `/api/snapshots/<id>/image.bin` | 下载该进度的救援镜像（缺失补零） |
| GET | `/api/snapshots/<id>/missing.json` | 下载同一进度的缺失扇区清单 |
| GET | `/api/gpt` | 用当前已救扇区核验主/备 GPT，返回决策与分区/缺失编号 |
| GET | `/api/gpt/partitions/<槽位>/raw` | 复查 job/GUID/范围/缺失后下载分区原始字节（query 带 `job_id,guid,type_guid,start,end`） |
| GET | `/api/demo/sample.img` | 下载内置 GPT 演示样盘 |
| POST | `/api/demo/setup` | 一键建演示任务并模拟首轮读盘（已有任务时 409） |
| POST | `/api/demo/read` | 演示补读：从已上传样盘取指定扇区上报；`{"sectors":[]}`/省略为全部缺失 |

`/api/gpt` 的顶层 `status`：

- `ok`：有且仅有一份可用表（或两份一致），`source` 标明 `primary`/`backup`，
  `partitions` 为可下载分区，每个分区带 `missing_sectors/missing_count/complete`；
- `conflict`：两份 GPT 都合法但布局不同，`differences` 给出逐条差异，停止提取；
- `invalid`：两份都已读出但 CRC/边界核验失败，`reasons` 说明原因；
- `incomplete`：至少一份 GPT 结构尚缺扇区，各副本的 `missing_sectors`
  给出待补编号，补读后重新请求即可。

分区下载的 `409` 响应码：`job_changed`（任务已换，提示刷新整页）、
`partition_changed`（GUID/范围有变，刷新分区列表）、`gpt_conflict` /
`gpt_invalid` / `gpt_incomplete`（表状态已不允许提取）、
`partition_incomplete`（带 `missing: [绝对扇区号...]`）。

上报扇区两种方式：

```bash
# 1) JSON 批量，data 为 base64
curl -X POST http://localhost:8000/api/sectors \
  -H 'Content-Type: application/json' \
  -d '{"sectors":[{"sector":12,"data":"<base64, 恰好 512 字节解码后>"}]}'

# 2) 单扇区原始二进制
curl -X POST 'http://localhost:8000/api/sectors?sector=12' \
  --data-binary @sector12.bin
```

快照下载：

```bash
SID=$(curl -s -X POST http://localhost:8000/api/snapshots | jq -r .snapshot.snapshot_id)
curl -OJ http://localhost:8000/api/snapshots/$SID/image.bin
curl -OJ http://localhost:8000/api/snapshots/$SID/missing.json
```

## 模拟读盘

```bash
python3 scripts/sim_reader.py --base http://localhost:8000 \
  --image ./sample.img --fail 3,7,1024 --rounds 3 --workers 4
```

每轮开始拉取缺失清单，只补读仍缺失的扇区；`--fail` 列出本轮读不出的扇区，
失败即跳过。

## GPT 提取与样盘演示

**网页最快路径**：`docker compose up --build` 后打开 http://localhost:8000，
在第 5 区点“一键载入 GPT 演示样盘”。首轮数据对应一个刻意设计的局面：

- 主 GPT（LBA 1 表头 + LBA 2..33 数组）全部未救出；
- 备 GPT（LBA 1023 表头 + LBA 991..1022 数组）完整有效，列出 3 个分区；
- 分区内坏扇区 `100`、`250,400`、`700,900` 分别落在三个分区中。

因此页面显示“仅凭备 GPT 提取”。在分区行里可看到 GUID、起止 LBA 与缺失
编号；点“演示补读缺失扇区”收齐后即可“下载原始字节”。点 GPT 副本诊断里的
“演示补读这些扇区”补回 LBA 1..33 后，主表（合法但更短的第 2 分区、没有
第 3 分区）与备表冲突，页面转为红色冲突说明并停止下载。

**命令行等价演练**：

```bash
python3 scripts/make_sample.py           # 生成 samples/sample-gpt.img 并打印 --fail 列表
# 第 1 轮：跳过 1..33 与 5 个坏扇区 -> /api/gpt 为 ok/source=backup
python3 scripts/sim_reader.py --image samples/sample-gpt.img \
  --fail 1,2,...,33,100,250,400,700,900
# 第 2 轮：只补坏扇区（继续 --fail 1..33）-> 分区收齐、可下载
# 第 3 轮：不带 --fail 补回主 GPT -> /api/gpt 为 conflict
```

注意：演示用的 `/api/demo/read` 是从**已上传的样盘文件**取数来模拟恢复
工具成功读盘；真实故障作业应通过第 3 节的 `/api/sectors` 上报实际读出的
数据，GPT 核验逻辑对两种数据来源完全一致。

## 测试

```bash
python3 -I tests/run.py               # 63 个单元/接口测试（含 25 个 GPT 用例）
python3 -I tests/integration_test.py  # 中断重启补读、边读边下载
python3 -I tests/gpt_demo_test.py     # 起真实 HTTP 服务自测备表提取/补读/并发/主备冲突
python3 -I tests/smoke_test.py        # 经 sim_reader 走真实 HTTP 的冒烟
```

## 设计说明与边界

- gunicorn 默认 **1 进程 8 线程**：快照保存在进程内存中，单进程可保证镜像
  与清单严格同进度；所有存储操作由同一把可重入锁串行化，线程安全。
- `job.json` 只是缓存，任何时刻都可以从 `sectors/` 重建，因此崩溃后不会
  出现「状态说有、文件没有」的幻影像。
- 快照保存在内存中，进程重启后旧 `snapshot_id` 失效（重新建快照即可）；
  已救出的扇区数据不受影响。
- GPT 分析是无状态的按需计算：每次 `/api/gpt` 和每次分区下载都从当前
  已救扇区重新核验，不缓存旧分区表，因此“点击下载时复查”与最新进度一致。
  `job_id` 随任务持久化在 `job.json`，重置/换任务即变化。
- 分区下载先在内存拼出整个分区（全盘上限 8 MiB），拼接期间只逐扇区短锁，
  不阻塞其他扇区上报；任务在下载途中被重置的极端情况下返回 `job_changed`。
- 样盘最大 8 MiB 是按题目要求设定的硬限制。
- 本工具只负责按 GPT 表边界提取分区原始字节，不解析分区内文件系统。
