# 跨仓分区对账的规范袋摘要与范围下钻

本项目保存湖仓治理的合成资料、冻结清单和读取工具。`lake_data.io` 读取 JSON
清单与资产身份，`lake_data.domain` 检查基本字段并给数量概况，`lake_data.codec`
实现冻结 schema 的规范编码、类型序与边界树，`lake_data.bag` 做单次遍历的外排
袋摘要，`lake_data.summary`/`lake_data.diff` 组合摘要并做范围袋差分，
`lake_data.reconcile` 串起对账，`lake_data.cli` 输出报告。字段、类型、时间与
版本口径见[资料约定](docs/data-contract.md)。设备、班组、主体和电量均为虚构，
不连接电网、生产数仓或人员系统。

Python 3.11 及以上，无第三方 Python 依赖。在项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q lake_data tests
python -m lake_data.cli inspect data/normal.json
python -m lake_data.cli bag data/normal.json
python -m lake_data.cli bag data/normal.json --keep   # 保留封存段供逐段复核
```

`inspect` 只显示原字段概况，不修改源资料。`bag` 报告包含：索引绑定（编码版本、
schema 摘要、边界树摘要、快照、源与段 SHA256）、摘要树（每节点覆盖区间与摘要）、
下钻范围、实际读取统计（含每侧段数、峰值打开文件数与缓冲条目数）、以及每段
完整有序袋差分。每段差分带 `ordered_diff_sha256`，可按文档说明独立重算。根不同时
只展开摘要不一致的叶；两侧源各只读取一次。`data/normal.json` 演练左侧重复两次
``b``、右侧缺该重复、右侧多 ``c``、右侧 ``é`` 为组合形式而左侧为预组合形式；
`data/same-source-bag.json` 证明相同多重集时根一致；`data/empty-partition.json` 证明空分区有标准摘要。
`invalid-config.json` 是基本配置错误，其余 JSON 为有效清单但可含需要领域操作
判断的边界资料。

空行、未知字段、缺必选字段、非法 decimal、非 UTC 时间戳都会使摄取失败；失败
时不发布根摘要。损坏段（截断、魔数、长度或 SHA256 不符）直接报错，不被当成
空区间。

Dockerfile 固定 Linux 基础镜像。`docker build -t lake-material .` 构建，
`docker run --rm lake-material` 读取正常清单；
`docker run --rm lake-material python -m unittest discover -s tests -v` 执行全部
检查。所有资料本地可读，无需账号和在线来源。
