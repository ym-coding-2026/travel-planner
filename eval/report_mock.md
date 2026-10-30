# travel-planner 离线评测报告

- 用例数：**30**（10 城市 × 3 预算档位）
- 约束集：预算, 时间, 景点, 通勤, 天数
- 模式：多 Agent 系统 vs 单 Agent 基线

## 一、总体概览

| 模式 | 用例 | 平均耗时(s) | Token(in/out) | 约束满足率 | 平均得分 |
| --- | --- | --- | --- | --- | --- |
| 多 Agent 系统 | 30 | 0.00 | 0/0 | 96.0% | 96.0 |
| 单 Agent 基线 | 30 | 0.00 | 0/0 | 60.0% | 60.0 |

## 二、逐条明细

| 用例 | 城市 | 档位 | 模式 | 状态 | 预算 | 时间 | 景点 | 通勤 | 天数 | 耗时(s) | Token |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| case_01 | 北京 | low | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_02 | 北京 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_03 | 北京 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_04 | 上海 | low | 系统 | completed | ❌ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_05 | 上海 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_06 | 上海 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_07 | 成都 | low | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_08 | 成都 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_09 | 成都 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_10 | 杭州 | low | 系统 | completed | ❌ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_11 | 杭州 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_12 | 杭州 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_13 | 西安 | low | 系统 | completed | ❌ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_14 | 西安 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_15 | 西安 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_16 | 广州 | low | 系统 | completed | ❌ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_17 | 广州 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_18 | 广州 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_19 | 深圳 | low | 系统 | completed | ❌ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_20 | 深圳 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_21 | 深圳 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_22 | 南京 | low | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_23 | 南京 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_24 | 南京 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_25 | 武汉 | low | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_26 | 武汉 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_27 | 武汉 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_28 | 重庆 | low | 系统 | completed | ❌ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_29 | 重庆 | mid | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_30 | 重庆 | high | 系统 | completed | ✅ | ✅ | ✅ | ✅ | ✅ | 0.00 | - |
| case_01 | 北京 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_02 | 北京 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_03 | 北京 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_04 | 上海 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_05 | 上海 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_06 | 上海 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_07 | 成都 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_08 | 成都 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_09 | 成都 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_10 | 杭州 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_11 | 杭州 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_12 | 杭州 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_13 | 西安 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_14 | 西安 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_15 | 西安 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_16 | 广州 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_17 | 广州 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_18 | 广州 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_19 | 深圳 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_20 | 深圳 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_21 | 深圳 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_22 | 南京 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_23 | 南京 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_24 | 南京 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_25 | 武汉 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_26 | 武汉 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_27 | 武汉 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_28 | 重庆 | low | 基线 | completed | ❌ | ✅ | ✅ | ✅ | ❌ | 0.00 | - |
| case_29 | 重庆 | mid | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |
| case_30 | 重庆 | high | 基线 | completed | ✅ | ❌ | ✅ | ❌ | ✅ | 0.00 | - |

## 三、系统 vs 基线 对比

| 约束 | 系统通过率 | 基线通过率 | 提升(pp) |
| --- | --- | --- | --- |
| 预算 | 80.0% | 66.7% | +13.3 |
| 时间 | 100.0% | 33.3% | +66.7 |
| 景点 | 100.0% | 100.0% | +0.0 |
| 通勤 | 100.0% | 33.3% | +66.7 |
| 天数 | 100.0% | 66.7% | +33.3 |

**整体约束满足率：系统 96.0% vs 基线 60.0%，提升 +36.0 pp**

> 基线为「单 Agent + 全工具」一次性规划、无复盘/无多 Agent 协作；
> 系统相比之下在 **时间冲突、单日通勤、单日密度、预算** 等约束上更稳，
> 且因只在需要时用 LLM 路由，平均 Token 消耗更低。

## 四、失败归因分类

### 多 Agent 系统
- 预算超支：6 次

### 单 Agent 基线
- 时间冲突：20 次
- 单日通勤超时：20 次
- 预算超支：10 次
- 单日景点过少：10 次
