# 车辆安全召回与修复跟踪系统

标准库 Python 3.11+ + SQLite。支持召回草稿、监管审核发布、范围待审修正（企业提交、监管通过后生效）、车辆登记与跨境流转、维修网点零件库存、修复证据复核、未完成高风险车辆统计，以及通知和监管上报版本。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8213`。身份通过 `X-Actor` 与 `X-Role` 请求头模拟，角色为 `manufacturer`、`regulator`、`dealer`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/dealers`、`POST /api/vehicles`：登记网点和车辆。
- `POST /api/vehicles/{vin}/transfer`：更新车辆所在国家和车主。
- `POST /api/recalls`、`POST /api/recalls/{id}/submit`：创建并提交召回。
- `POST /api/recalls/{id}/review`：监管发布或退回。
- `POST /api/recalls/{id}/amendments`：企业提交范围待审修正（新范围、原因、版本号、请求键）；`POST /api/recalls/{id}/scope` 为兼容别名。监管通过前原范围照常执行，相同请求键的重复申请沿用首次结果。
- `POST /api/amendments/{id}/review`：监管审核范围修正。通过后新增车辆进入通知和上报队列，移出车辆的未完成通知撤销，已确认维修保留但不再计入未完成。
- `GET /api/recalls/{id}/amendments`：查看待审范围、差异和处置结果。
- `POST /api/recalls/{id}/parts`：维修网点入库。
- `POST /api/repairs`、`POST /api/repairs/{id}/review`：报告并复核维修。
- `GET /api/recalls/{id}/unfinished`：查看高风险未完成车辆。
- `GET /api/state`、`GET /api/health`：状态与健康检查。

## 范围待审修正

发布后企业不再直接覆盖范围：修正先登记为待审版本（含原因、差异和请求键），监管通过后才写入召回并归档到 `scope_changes`（含原因和修正单号）。差异计算、版本留档和接口处理分别在 `scope_diff.py`、`scope_amendments.py`、`scope_api.py` 三个业务文件。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为本地原型：跨境规则用许可字符串模拟，零件库存与维修记录是简化模型，不包含真实 VIN 解码、监管接口、物流系统或法定通知渠道。
