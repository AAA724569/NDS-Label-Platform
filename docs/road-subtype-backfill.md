# 道路子类视觉推断补标

`road_subtype_backfill.py` 对已完成批次使用原代表帧补充道路子类，模型仍为
`deepseek-flash`，提示词版本为 `road-subtype-visual-v1`。本次授权允许根据道路结构、
周边环境和出入口等图像线索选择最接近的现有子类；结果是视觉推断，不是法定或规划等级核验。
未见道路或图像无法判断时保留 unknown，并终止该来源修订的自动尝试。

只处理 `completed`、`validation_passed=true`、`pending`、完整27字段契约有效、
子类为 unknown 的批次；代表视频必须有效且属于该批次。已审核通过、已退回或不完整的批次不会进入队列。
已观测的大类完整保留；大类也是 unknown 时，仅允许模型同时给出相互匹配的大类与子类后一起更新。
大类为 unlabeled、not_observed 或 not_applicable 的批次不处理。

## 执行

使用现有私有环境文件与运行环境，先执行一个批次，再按需清空剩余队列：

```bash
/home/stu1/Projects/NDS-Statistics-Platform/.venv/bin/python \
  road_subtype_backfill.py --env-file /home/stu1/.config/nds-annotation-worker.env \
  --once --concurrency 1

/home/stu1/Projects/NDS-Statistics-Platform/.venv/bin/python \
  road_subtype_backfill.py --env-file /home/stu1/.config/nds-annotation-worker.env \
  --concurrency 4
```

默认4个线程，可用 `--concurrency` 设置1至32。每个线程独立持有数据库连接。
`--max-batches N` 或 `--limit N` 限制本次领取的尝试次数；`--once` 等价于限制一次。
次数包括临时失败重试，便于控制试运行规模。无上限时，任务处理当前所有符合条件的批次后退出。
任务是有界补标程序，不负责周期性发现新视频；后续新批次由常规 worker 的 `battery-frame-v4`
全字段提示词执行同样的视觉推断政策。

服务器批量任务可安装 `deploy/systemd/nds-road-subtype-backfill.service` 后手动启动。
该单次队列服务使用32个并发请求，清空当前队列后退出，不需配置开机启动；后续新批次继续由常规标注服务处理。

独立会话锁 `634129872542` 阻止同一补标任务重复运行，常规全字段 worker 可继续工作。
每次领取均在短事务中使用 `FOR UPDATE SKIP LOCKED`，领取后才执行图片读取和模型请求。
领取中的 labeling 任务不会因并发修改产生的新修订被再次领取；旧任务完成来源冲突处理后，
已重新校验的新修订可以领取，并在图片和原大类不变时直接重用缓存。
模型调用期间，顶层 completed 状态与原校验标记保持有效，现有可发布结果持续可读。

## 证据、写入与历史

程序只读取数据库已有 `frame_path`，发送前后验证图片实际 SHA-256 与原记录一致；
不会重新选帧、修改视频或更新代表帧证据。模型仅返回大类与子类两项。
子类 observed 必须携带以“视觉推断：”开头的原因；原本 unknown 的大类改为 observed 时同样要求。
其余字段的观测、测量限制与枚举契约保持现有规则。

写入前重新锁定当前批次并比较领取令牌、完整原标签、修订号、代表视频、时间点、图片路径、
ROI、补帧与图片摘要，同时核查当前批次状态及代表视频有效性。发生变更时保留当前结果，
将本次输出记为 `source_changed`。合法更新只替换子类，或同时替换原先 unknown 的大类。
合并后的完整27字段必须通过现有校验；标签更新触发修订递增后，在同一事务的第二次更新恢复
`validation_passed=true`。不会把视觉结果伪装成人工审核。

原始全字段 `raw_model_response`、`model_name` 与 `prompt_version` 不变。
`processing_info.road_subtype_backfill` 保存当前补标状态、提示词版本、领取令牌、尝试次数及输入/输出修订；
`processing_info.road_subtype_history` 追加每次尝试的原/新大类子类、原始补标响应、实际模型、
提示词与契约版本、图片摘要、请求摘要、时间、用量、缓存命中和结果状态。历史与模型原文只保存在私有存储。

缓存位于输出根目录下的 `<batch_id>/road-subtype/subtype-<prompt_version>-<request_sha256>.json`。
请求摘要包括图片、模型、契约版本、完整原大类及提示词摘要。缓存目录权限0700、文件0600；
使用临时文件和原子创建保留首次响应，不覆盖已有缓存。有效缓存可在事务冲突或中断后重用，避免重复请求。

## 重试与退出

限流、临时服务不可用和超时最多尝试3次，间隔60秒；中断后重新启动会恢复未完成的领取，
仍受3次上限约束。第3次已保存缓存但未提交数据库就中断时，恢复进入 cache_replay，
仅重放匹配缓存且保持尝试次数为3；缓存缺失或无效则终止，绝不发出第4次模型请求。
认证或额度错误记录安全错误码后停止整个任务，退出码78。
校验失败、无可推断结果、证据异常和已达重试上限均为当前来源修订的终态，不自动循环付费请求。
SIGTERM / SIGINT 阻止继续领取，已发出的请求可完成校验与事务提交；数据库与图片原始信息不进入日志。

现有统计发布器直接读取 `completed AND validation_passed` 的有效自动结果，排除明确退回。
合并标签会改变语义摘要，由既有发布定时器自动发布，无需新增审核流程或数据库迁移。

## 验证

专用测试使用临时 PostgreSQL Unix socket 和55443端口，模型边界由固定响应替代，
不会读取生产凭据、连接生产数据库或请求真实模型：

```bash
/home/stu1/Projects/NDS-Statistics-Platform/.venv/bin/python -m pytest \
  tests/test_road_subtype_backfill.py -q
```

测试涵盖子类及 unknown 大类合并、原26/25字段与全字段模型原文保留、并发变更保护、
独立连接并行处理、发布状态保留、图片摘要、私有缓存重用、终态 unknown、重试次数与退避、
中断恢复、认证失败停止、领取上限以及重复任务锁。
