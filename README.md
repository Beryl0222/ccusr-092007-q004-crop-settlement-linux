# 节令原料保底结算

合作社协调小农户节令原料（芝麻、花生、果仁）的保底收购、浮动分级验收、
不可抗减产协商与差额结算。

## 数据合同与迁移

`fixtures/supply_commitment.json` 保存经过脱敏的业务样例。信封字段
（`schema_version` / `record_id` / `domain` / `occurred_at` /
`revision` / `source`）的标识与时间含义保持不变。

- **revision 1**：仅信封字段。`load_record()` 继续支持，
  `load_dataset()` 对其返回空仓储，不做破坏性迁移。
- **revision 2（当前）**：在同一信封下追加业务段：
  `parties`、`stations`、`plots`、`batches`、`commitments`（条款版本化）、
  `advances`、`deliveries`、`samples`、`gradings`、`dispositions`、
  `fm_events`、`negotiations`、`consumptions`、`adjustments`、`appeals`。
  迁移方式：旧文件无需改写；读取 revision 2 文件才填充业务数据，
  新状态均为新增字段或新实体，不覆盖既有信封。

## 核心业务规则

- **版本化承诺**：保底价、分级价、交付窗口、付款账期等条款是不可变快照；
  价格或规则变化必须发起新版本，新版本在农户明确同意前不生效，
  同意后旧版本冻结为 `superseded`。结算只依据最近一个 `agreed` 版本。
- **原批次贯穿**：称重、封样、站点初检、实验室更正、部分接收、退货、
  替代去向全部沿用 `batch_id` / `commitment_id`。实验室更正不新开单据，
  旧检验记录置 `corrected` 并指向新记录。
- **数量守恒与并发**：同一交付的全部处置（接收/退货/替代去向）数量之和
  必须等于净重；仓储在单把全局锁内完成读-校验-写，多站点并发登记时
  超出净重的最后一笔收到 `409 quantity_overflow`，实收数量永不被突破。
  同一承诺下票号唯一，防止重复登记。
- **替代去向**：仅当条款允许且登记了农户明确同意与目的地时才成立，
  工坊按条款收取代办费，不计货款。
- **不可抗减产协商**：洪涝/持续干旱事件必须附证据并经合作社确认；
  协商必须附减产证据，结论把减产总量拆成**不可抗部分**与**未履约部分**：
  前者对应预付可减免、互不追偿，后者对应预付由农户退还（单独挂账）。
  未达成一致的协商记 `rejected`，不得据此减免。
- **总账复算**：`compute_settlement()` 是确定性纯计算，由
  分级货款、保底价补差（分级单价低于保底价的部分）、扣减、替代去向代办费、
  已付预付款、不可抗减免/退还、工坊分担、申诉调整组成；
  尾款为正由工坊支付，农户应退部分单独列 `advance_receivable`。
- **农户可见**：每次交付后立即可查数量、毛/皮/净重、最新有效等级
  （含实验室更正标记）、逐条扣减理由与预计付款日。
- **成品反查**：成品批次的投料行可反查原料批次、交付单与承诺结清状态；
  工坊只能登记和查看本工坊采购的原料。
- **申诉**：农户可对检验/扣减/退货/结算发起申诉并附证据，合作社裁决
  成立时生成总账调整，下一次复算自动入账。
- **采购关系隔离**：工坊令牌只能访问自己作为采购方的承诺；
  跨关系访问统一返回 404，不泄露关系是否存在。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `src/crop_settlement/contracts.py` | 数据合同读取、revision 1/2 兼容加载 |
| `src/crop_settlement/models.py` | 枚举、实体、序列化编解码 |
| `src/crop_settlement/store.py` | 线程安全仓储、乐观锁、采购关系鉴权 |
| `src/crop_settlement/services.py` | 全部领域规则与总账复算 |
| `src/crop_settlement/api.py` | 标准库线程化 HTTP API |

## HTTP API（快速参考）

请求头 `Authorization: Bearer <party token>`（`/health` 除外）。

- `POST /commitments`、`POST /commitments/{id}/agree`、`POST /commitments/{id}/revisions`
- `POST /commitments/{id}/advances`
- `GET  /commitments/{id}/settlement`、`POST /commitments/{id}/close`（合作社）
- `POST /deliveries`、`GET /deliveries/{id}`（农户交付清单）
- `POST /deliveries/{id}/samples`、`POST /samples/{id}/gradings`、`POST /gradings/{id}/corrections`
- `POST /deliveries/{id}/dispositions`
- `POST /fm-events`（合作社）、`POST /negotiations`、`POST /negotiations/{id}/resolution`（合作社）
- `POST /appeals`（农户）、`POST /appeals/{id}/resolution`（合作社）
- `POST /consumptions`、`GET /products/{lot}/trace`

错误响应统一为 `{"code": ..., "message": ...}`，并发超收为
`409 quantity_overflow`，跨工坊访问为 `404 not_found`。

## 本地检查

```bash
python -m unittest discover -s tests
```
