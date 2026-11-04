# 第 1 篇：核心场景与排障剧本（Use Cases）

> **文档目的**：在编写任何代码、定义状态或设计数据库之前，先用最直白的业务剧本定义清楚：**系统会遇到哪几类典型故障、真人工程师是怎么查的、Agent 应该怎么模仿这个过程、以及最终交付给用户什么结果。**

为了覆盖真实后端开发中最常见的排障情况（同时应对面试官的不同追问），本项目核心支持以下 **3 类标准排障剧本**：

---

## 剧本一：运行时崩溃故障（有 ERROR 日志 + 需结合代码与数据库联合求证）

> **代表性**：★★★★★（最核心的主流程，展示“ES 日志 + 本地源码 + MySQL 数据”三维交叉验证与代码修复闭环）

### 1. 触发输入（Input）
客服或监控系统发来一句模糊描述（没有告知哪个文件报错）：
> *“刚刚用户 `user_id=10086` 反馈在购物车点击提交订单时，页面提示‘系统内部异常（500）’，帮我排查原因并修复。”*

### 2. 期望的排查过程（Investigation Steps）
1. **第 1 步（查历史库）**：先去历史故障库检索“提交订单 500”，未发现完全相同的历史故障。
2. **第 2 步（查 ES 日志锁现场）**：
   * 拿着线索 `"10086"` 和级别 `"ERROR"` 去 Elasticsearch 搜索最近 30 分钟的日志；
   * 捞出报错堆栈，拿到关键信息：
     * 请求路径：`POST /api/orders/checkout`
     * 报错位置：`app/services/order_service.py` 第 64 行
     * 异常文本：`TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'`
   * 系统自动将该条日志登记为证据 **`[ev_log:01]`**。
3. **第 3 步（提假设并双路求证）**：
   看到第 64 行 `float - NoneType`，Agent 提出两个假设并同时去查：
   * **去本地代码目录看源码（代码探针）**：调用 `read_file("app/services/order_service.py", offset=45, limit=30)`，看到第 64 行写着：
     ```python
     final_price = total_price - coupon.discount_amount
     ```
     登记为证据 **`[ev_code:02]`**。
   * **去 MySQL 查该用户的数据（数据探针）**：调用只读 SQL 查询用户 `10086` 当前使用的优惠券记录：
     ```sql
     SELECT id, coupon_type, discount_amount, discount_rate FROM coupons WHERE user_id = 10086 AND status = 'UNUSED';
     ```
     查出该用户用的是一张**折扣券（`coupon_type='RATE'`, `discount_rate=0.8`）**，所以它的固定满减金额字段 `discount_amount` 在数据库里是 `NULL`。登记为证据 **`[ev_db:03]`**。
4. **第 4 步（得出确凿根因）**：
   将代码 `[ev_code:02]` 与数据库记录 `[ev_db:03]` 一对比，真相大白：数据库数据本身是合法的（折扣券本来就没有固定减免额），Bug 出在 `order_service.py` 第 64 行没有判断 `coupon.coupon_type`，直接拿 `total_price` 减去 `None` 导致崩溃。

### 3. 最终交付物（Deliverables）
1. **结构化排障报告（`IncidentReport`）**：
   * **摘要（Summary）**：用户 10086 使用折扣型优惠券下单时，订单服务未区分优惠券类型，直接读取为 `None` 的 `discount_amount` 字段引发 `TypeError` `[ev_log:01, ev_code:02, ev_db:03]`。
   * **根因（Cause）**：`app/services/order_service.py:64` 缺少对 `coupon_type == 'RATE'` 的分支处理 `[ev_code:02]`。
2. **代码修复补丁（Diff Patch）与单测结果**：
   ```diff
   - final_price = total_price - coupon.discount_amount
   + if coupon.coupon_type == "RATE" and coupon.discount_rate is not None:
   +     final_price = round(total_price * coupon.discount_rate, 2)
   + else:
   +     final_price = total_price - (coupon.discount_amount or 0.0)
   ```
   * 沙箱单元测试结果：`pytest tests/test_order_service.py` -> **PASSED**（待人工点击“批准”后写入文件）。

---

## 剧本二：静默逻辑缺陷（无任何报错日志，靠代码搜索与 Git 历史破案）

> **代表性**：★★★★☆（专门应对面试官追问：“如果程序没崩、日志里没有 ERROR 堆栈，只是业务逻辑算错了，你的 Agent 怎么查？”）

### 1. 触发输入（Input）
> *“测试反馈调用 `/api/orders/preview` 预览订单价格时，等级为 `vip_level=2`（黄金会员）的用户没有享受到 9 折会员价，接口返回状态码是 200 正常，没有任何报错。”*

### 2. 期望的排查过程（Investigation Steps）
1. **第 1 步（查 ES 日志）**：去 ES 搜 `/api/orders/preview` 的 ERROR 日志，返回为空（确认无崩溃异常）。
2. **第 2 步（全局搜路由锁入口）**：
   * 既然没有报错行号，Agent 调用 `grep(pattern="/orders/preview", path="app/")` 在本地目录搜接口 URL；
   * 定位到入口文件 `app/api/order_router.py:28`，顺藤摸瓜看到它调用了 `pricing_service.calc_vip_discount(user, amount)`。
3. **第 3 步（读业务代码 + 查 Git 最近是谁改的）**：
   * 打开 `app/services/pricing_service.py` 读取 `calc_vip_discount` 函数，发现第 42 行写着：
     ```python
     if user.vip_level > 2:  # 只有大于 2 才打折，把等于 2 的黄金会员漏掉了！
         return amount * 0.9
     ```
     登记为证据 **`[ev_code:01]`**。
   * 调用 `git_blame_diff("app/services/pricing_service.py", 38, 48)`，发现昨天提交 `commit 9c2b10` 在重构时把 `>= 2` 误改成了 `> 2`。登记为证据 **`[ev_git:02]`**。

### 3. 最终交付物（Deliverables）
1. **结构化排障报告**：明确指出是 `commit 9c2b10` 引入的边界条件退化缺陷 `[ev_code:01, ev_git:02]`。
2. **代码修复补丁**：将 `user.vip_level > 2` 修复为 `user.vip_level >= 2`，并跑通边界值单元测试。

---

## 剧本三：历史复发与环境配置故障（命中 CRAG 历史库 + 不乱改业务源码）

> **代表性**：★★★★☆（展示 Agent 的克制性与 CRAG 知识库价值：不是所有问题都要改代码，遇到发生过的配置/外部问题直接秒级给出运维方案）

### 1. 触发输入（Input）
> *“告警：过去 5 分钟短信发送接口 `/api/notify/sms` 出现大量 `ConnectTimeoutError` 超时异常。”*

### 2. 期望的排查过程（Investigation Steps）
1. **第 1 步（查历史故障库 CRAG）**：
   * 调用 `search_incidents(query="notify/sms ConnectTimeoutError")` 去 Qdrant + ES 检索；
   * **直接命中**上个月的历史故障复盘单 `INC-2026-089`（登记为证据 **`[ev_hist:01]`**），里面记载：*“第三方短信网关在整点促销高峰期并发限制为 50 QPS，超出后对方直接丢包导致连接超时，需在配置文件中开启异步队列削峰开关 `SMS_ASYNC_QUEUE_enabled=true`。”*
2. **第 2 步（核实当前配置与日志）**：
   * 查 ES 确认报错现象与 `INC-2026-089` 完全一致 **`[ev_log:02]`**；
   * 调用 `read_file("config/settings.yaml")` 检查当前配置，发现 `sms_async_queue_enabled: false` **`[ev_code:03]`**。

### 3. 最终交付物（Deliverables）
1. **结构化排障报告**：
   * 在 `previous_occurrence`（历史复发栏）中明确指出：**“本次故障为历史故障 `INC-2026-089` 的复发 `[ev_hist:01]`”**。
   * 在 `next_steps` 中给出修改 `config/settings.yaml` 开启异步队列开关的建议 `[ev_hist:01, ev_code:03]`，**不随意篡改 Python 业务代码**。

---

## 总结：从这 3 个剧本里，我们直接推导出了后续设计所需的全部要素

写完这 3 个剧本，你会发现后面第 2、3 篇文档要写什么已经一清二楚了：
1. **需要哪些工具（Tools）？**
   * 历史检索：`search_incidents`
   * 日志查询：`search_error_logs`
   * 本地目录与代码：`ls`、`grep`、`read_file`、`git_blame_diff`、`edit_file`、`execute`（跑 pytest）
   * 数据库核验：`execute_readonly_sql`
2. **报告里需要哪些字段（Schema）？**
   * 必须有 `previous_occurrence`（应付剧本三）、`hypotheses`（应付剧本一的多方向排查）、`evidence_ids`（防瞎猜）、以及 `patch_diff`（应付剧本一、二的代码修复）。
