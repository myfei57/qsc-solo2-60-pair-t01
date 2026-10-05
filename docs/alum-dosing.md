# 加矾（铝盐）自动投加系统 — 设计与策略规格

本模块把"进水流量 × 固定系数"替换为按 **流量 + 进水浊度 + 水温 + 矾液浓度** 的
完整闭环，并对班次浓度标定、刷新时序、抢投策略、计量口径、审计和控制台展示
给出唯一、可测试的定义。

代码位置：`waterplant/coag/`（`model.py` 算式、`concentration.py` 班次标定、
`engine.py` 决策引擎、`trail.py` 审计链、`clock.py` 时钟）；
HTTP 面：`waterplant/console/alum_handlers.py`、`alum.py`。

---

## 1. 投加量怎么算（固定口径）

| 量 | 单位 | 说明 |
|---|---|---|
| 进水流量 Q | m³/h | 取入厂流量计 |
| 进水浊度 T | NTU | 取进厂浊度仪 |
| 水温 t | ℃ | 取原水温度计 |
| 比投加量 K | mg 矾 / L 水 | 浊度分段折线查表 × 水温系数 |
| 矾液浓度 C | %(m/m) | 本班次标定值（版本化） |
| 矾液密度 ρ | g/mL | 配置项，默认 1.12 |
| 矾液流量 Q_sol | mL/min | 计量泵要打的药液量 |
| 泵冲次 S | 冲/min | Q_sol ÷ 每冲体积，经限位/限速 |

计算链（每一步都原样写入决策与审计）：

```
K_base  = curve_turbidity(T)          # 浊度—比投加量折线（拐点插值，两端钳平）
f_temp  = curve_temperature(t)        # 水温系数：冬季最高 +20%，≥20℃ 为 1.0
K       = K_base * f_temp * uplift    # uplift 仅在 STALE/NONE/浊度仪长期丢失时出现
m_dot   = K * Q * 1000 / 60           # mg/min
Q_sol   = m_dot / (ρ * C * 10)        # mL/min；C 用百分数
S_raw   = Q_sol / stroke_volume_ml
S_cmd   = 限速/死区/最大冲次/断流关断后的实际指令
```

关键推论（已有测试锁定）：**矾液浓度减半，同工况药液量翻倍**——这正是老
固定系数方案冬天欠投、配液稀释后过量的根因，现在由浓度项显式补偿。

浊度折线、水温折线、泵参数、阈值均在 `AlumConfig`（`coag:alum:config` 键）
中持久化，可经 `GET/PUT /alum/config` 调整，修改时做完整校验；**投加代码
本身不含任何可调常数**。

### 瞬时值还是一段均值？——按模式定死

| 模式 | 流量 | 浊度 |
|---|---|---|
| AUTO | 最近 N 拍（默认 5）滑动均值 | 同窗口均值 |
| GRAB（抢投） | **瞬时值** | 瞬时值 + 本轮风暴峰值保持（max-hold） |

不允许在单次决策里混用；每个决策快照带 `dose_basis=MEAN|INSTANT` 和原因码
（`AUTO_MEAN_BASIS` / `GRAB_INSTANT_BASIS`）。GRAB 退出（连续 3 拍浊度低于
退出阈值）后峰值保持清零。

---

## 2. 浓度按班次重新标定

* 班次长度默认 8 小时（配置项，UTC 对齐）；每个自然班次至多一张票。
* 标定是一张显式**工单（ticket）**：`OPEN → (读数…)`，每个班次需 N 个
  平行样（默认 3）。N 个读数齐了才取均值并发布新版本。
* 均值必须落在合理性区间 `[concentration_min_pct, concentration_max_pct]`
  内，否则工单 `REJECTED`，**在用浓度一个字都不改**。
* 浓度版本（`ConcentrationVersion`）不可变，含 `applied_at`、班次、工单号、
  上一版本 id，形成版本链。

### 读数丢失 = 部分失败，可恢复且恢复幂等

* 某个平行样丢失：`POST /alum/reading/lost` → 工单转 `READING_LOST`，
  已提交的读数全部保留，不重开整轮。
* 恢复：`POST /alum/recover` → 工单回 `OPEN`、清除丢失位、`recovery_count+1`。
  **重复触发恢复是幂等的**：对同一张已恢复/已接受的票再调，不产生新事件、
  不重复计数（测试 `test_lost_reading_is_partial_failure_with_idempotent_recovery`）。
* 补样沿用原 replicate 号；对同一 replicate 重复上报读数也是幂等 no-op。
* 对 `ACCEPTED/REJECTED` 终态票再报读数/报丢失一律 no-op，不会二次发版。
* 工单超过 `due_epoch` 未完成：下一拍 `maintain` 自动置 `READING_LOST` 并
  审计 `CALIB_OVERDUE`，等待恢复，不静默换口径。

### 刷新与投加之间的时序依赖（核心契约）

1. tick 先做班次日历维护，再以**本拍对齐时刻 `slot`** 做浓度 as-of 读取
   （`version_as_of(slot)`：只接受 `applied_at <= slot` 的版本）。
2. 新版本与 `CALIB_ACCEPTED` 审计行在**同一个 store 事务**里提交
   （`Store.transaction()`，RLock 临界区）。因此：
   * 正在接受标定的那一拍**一定**仍按旧口径算，且审计、决策、控制台看到的
     是同一份刷新前口径；
   * 下一拍才可能看到新口径，决策里同时记录 `used_pct / previous_pct /
     active_version_id / previous_version_id / age_seconds`；
   * 不可能出现"泵用了新浓度但审计里没有接受记录"这种撕裂。
3. 同一对齐 tick 的重复调用返回**已存储的原决策**（幂等，附
   `TICK_SLOT_REPLAY_RETURNED_STORED_DECISION`），不重算、不补审计；严格
   早于已执行 slot 的请求直接拒绝。

浓度态势（`situation`）及策略矩阵：

| 态势 | 判据 | 用什么浓度 | 附加动作 |
|---|---|---|---|
| FRESH | 有版本且未超 `staleness_seconds`（默认10h） | 版本浓度 | — |
| PENDING | 版本仍新鲜 + 本班次有未完成票 | **旧版本浓度** | AUTO/GRAB 均先顶住 |
| STALE | 最新版本已超期（无论是否有在途票） | 保守地板浓度 | 需求上浮 `stale_uplift`(默认+10%) |
| NONE | 从未接受过标定 | 地板浓度（默认8%） | 需求上浮 +10% |

审计原因码（节选）：`CONCENTRATION_FRESH`、`TICKET_OPEN/READING_LOST`、
`PENDING_KEEPS_OLD_UNTIL_CONFIRMED`、`VERSION_PAST_STALENESS`、
`NO_ACCEPTED_VERSION`、`STRENGTH_STALE_USES_FLOOR_PLUS_UPLIFT`、
`NO_CALIBRATION_USES_FLOOR_PLUS_UPLIFT`。

---

## 3. 抢投时：先用旧浓度顶，还是等新浓度确认？——按工况定死

由配置项 `grab_pending_use_old` 选择整条线的策略，**每次决策都把所选策略
写进原因码**，杜绝临场含糊：

| 工况 | `grab_pending_use_old=True`（默认，推荐） | `=False`（保守） |
|---|---|---|
| AUTO + PENDING | 旧浓度 + 均值，继续投 | 同左 |
| GRAB + PENDING，泵已有在跑指令 | 旧浓度 + 瞬时，**先顶住覆盖洪峰**（`GRAB_PENDING_USES_OLD_TO_COVER_SPIKE`） | **冻结当前冲次**，等新浓度确认（`GRAB_PENDING_FREEZES_PUMP_AWAITING_CONFIRMATION`） |
| GRAB + PENDING，尚无在跑指令 | 旧浓度先顶（绝不在暴雨里投零） | 退化为旧浓度先顶，原因码同时标注等待策略 |

进入 GRAB 的触发（任一）：浊度 ≥ `grab_enter_ntu`（默认200 NTU）、
浊度上涨速率 ≥ `grab_rate_ntu_per_min`（默认20 NTU/min）、或操作员
`POST /alum/grab/arm` 手动抢投。退出：自动模式下连续 `grab_exit_ticks`
拍低于 `grab_exit_ntu`（默认100），或手动 `POST /alum/grab/release`。
手动 arm/release 按"轮次号"做幂等键，抢投→解除→再抢投是独立审计事件，
同一轮内重复 arm 不重复记账。

### 传感器故障三态（与抢投正交，优先于一切投加理由）

| 通道 | 短期（≤`sensor_hold_seconds`，默认15min） | 长期 |
|---|---|---|
| 流量 | HOLD：冻结上一条泵指令（`FLOW_SENSOR_LOST_HOLDING_LAST_COMMAND`） | STALE：**安全关断**冲次归零（`..._PUMP_SHUTDOWN`） |
| 浊度 | HOLD：沿用最后可信值（`TURBIDITY_SENSOR_LOST_USING_LAST_KNOWN`） | STALE：沿用值但需求 +10%（`..._STALE_UPLIFT_APPLIED`） |
| 水温 | HOLD：沿用最后可信值（`..._USING_LAST_KNOWN`） | 无历史则按 20℃（`..._DEFAULT_20C`） |

计量泵另有：最大冲次钳制、每拍最大变化率（默认20 spm）、死区（默认3%）、
小信号断流（默认2%），每一步都在原因码里体现（`PUMP_CLAMPED_TO_MAX_STROKES`、
`PUMP_MOVE_RATE_LIMITED`、`PUMP_MOVE_WITHIN_DEADBAND`、
`PUMP_BELOW_CUTOFF_SWITCHED_OFF`）。

---

## 4. 一致性：投加量、刷新前后口径、判定原因同源

一个 tick 只产生**一个决策快照**（`engine.tick` 返回值即落库的
`last_decision`），审计行的 payload 就是这份快照本身，控制台 `/alum/last`、
`/alum/state`、`/snapshot`、文本面板读的也是它。因此：

* 投加量各分量自洽：`mass = K·Q·1000/60`，`Q_sol = m/(ρ·C·10)`，
  `S_cmd` 与限位规则一致（测试 `test_decision_payload_is_self_consistent`
  用重算断言锁定）。
* 口径刷新前后并列：`used_pct / snapshot_pct / active_version_id /
  previous_version_id / previous_pct / age_seconds / uplift`。
* 判定原因全部枚举在原因码常量里（`waterplant.coag.REASONS`），快照、审计、
  面板三处一致；健康检查 `alum_concentration` 直接据态势给 ok/warn/fail。

计量：以**上一拍指令**在拍间积分药液体积（指令当下生效，当拍不追补），
按自然日与累计双口径落账；若拍间隔超过 `max_integrate_seconds`（默认300s，
漏采/停机），**该段不积分**并记 `TICK_GAP_TOO_LONG_VOLUME_NOT_INTEGRATED`，
防止虚增药量。

## 5. 审计：哈希链 + 幂等

* 所有投加决策（`ALUM_DECISION`）与标定生命周期事件
  （`CALIB_OPENED/READING/READING_LOST/RECOVERY/ACCEPTED/REJECTED/OVERDUE`、
  `GRAB_ARMED/RELEASED`）追加到同一条只增链 `coag:alum:audit`。
* 每行含 `seq`、前一行 SHA-256、本行哈希；`GET /alum/audit/verify` 全链
  校验，删行/改行/乱序立刻可检出（见 `test_chain_detects_tampering`）。
* 幂等键：决策按对齐 tick（`ALUM_DECISION:{slot_index}`），标定事件按
  `类型:工单:稳定载荷`，恢复/抢投按轮次。崩溃重试不会产生重复行。

## 6. 控制台接口（节选）

```
POST /alum/tick            跑一拍 {flow,turbidity,temperature[,epoch]}；null=仪表丢失
GET  /alum/last  /alum/state  /alum/panel(文本面板)
GET  /alum/mode ; POST /alum/grab/arm | /alum/grab/release
POST /alum/tickets/open ; POST /alum/reading ; POST /alum/reading/lost
POST /alum/recover ; POST /alum/reject
GET  /alum/concentration ; /alum/versions ; /alum/tickets ; /alum/ticket
GET  /alum/audit ; /alum/audit/verify
GET|PUT /alum/config
```

所有状态落在既有原子写 JSON 文档 `Store` 中，进程重启后模式、窗口、峰值
保持、累计量、版本链全部恢复（`test_engine_state_persists_across_restart`）。
离线演示：`python -m waterplant.alum_demo`（假时钟，不碰真泵）。
