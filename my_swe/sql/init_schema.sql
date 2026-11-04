-- ============================================================================
-- 《04_DATABASE.md》完整 MySQL 建表与靶场初始化数据脚本
-- 包含：1. 平台管理库 6 张核心表  2. 微商城故障靶场库 3 张业务表与预设故障数据
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `rca_platform_db` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `rca_platform_db`;

-- 表 1: 被监控目标项目配置表
CREATE TABLE IF NOT EXISTS `workspaces` (
    `id` VARCHAR(64) NOT NULL COMMENT '目标项目唯一标识 (target_project_id)，如 mall_demo',
    `name` VARCHAR(128) NOT NULL COMMENT '项目显示名称',
    `local_repo_path` VARCHAR(500) NOT NULL COMMENT '本地代码库根目录绝对路径',
    `es_log_index` VARCHAR(128) NOT NULL COMMENT 'ES 运行日志索引名',
    `mysql_readonly_uri` VARCHAR(500) NOT NULL COMMENT '业务数据库只读连接串',
    `default_test_cmd` VARCHAR(255) NOT NULL DEFAULT 'pytest' COMMENT '默认单元测试命令',
    `repo_map_cache` MEDIUMTEXT NULL COMMENT '预生成的目录与函数签名拓扑树缓存',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='被监控目标项目与多源探针连接配置表';

-- 表 2: 排障会话主表 (id 兼任 LangGraph thread_id)
CREATE TABLE IF NOT EXISTS `incidents` (
    `id` VARCHAR(64) NOT NULL COMMENT '排障会话ID，直接兼任 LangGraph thread_id',
    `target_project_id` VARCHAR(64) NOT NULL COMMENT '关联目标项目ID (workspaces.id)',
    `title` VARCHAR(255) NOT NULL COMMENT '排障会话标题',
    `trigger_source` VARCHAR(32) NOT NULL DEFAULT 'manual_chat' COMMENT 'manual_chat / webhook_alert',
    `status` VARCHAR(32) NOT NULL DEFAULT 'investigating' COMMENT 'investigating / need_info / waiting_approval / resolved / inconclusive',
    `latest_report` JSON NULL COMMENT '最新通过防幻觉校验的 IncidentReport JSON',
    `report_digest` VARCHAR(64) NULL COMMENT '最新报告核心结论的 SHA-256 指纹',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_project_status` (`target_project_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='故障排障会话主表';

-- 表 3: 会话多轮消息历史表
CREATE TABLE IF NOT EXISTS `incident_messages` (
    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '消息自增主键',
    `incident_id` VARCHAR(64) NOT NULL COMMENT '所属排障会话ID',
    `turn_index` INT NOT NULL DEFAULT 1 COMMENT '对话轮次序号',
    `role` VARCHAR(32) NOT NULL COMMENT 'user / assistant / system',
    `msg_type` VARCHAR(32) NOT NULL DEFAULT 'text' COMMENT 'text / node_step / report_card / patch_approval',
    `content` MEDIUMTEXT NOT NULL COMMENT '消息正文内容',
    `metadata_json` JSON NULL COMMENT '扩展元数据 JSON',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_incident_turn` (`incident_id`, `turn_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='排障会话多轮对话与事件流记录表';

-- 表 4: 真实证据账本明细表
CREATE TABLE IF NOT EXISTS `incident_evidences` (
    `evidence_id` VARCHAR(64) NOT NULL COMMENT '系统自动签发的唯一证据编号，如 ev_log:8f3a1c',
    `incident_id` VARCHAR(64) NOT NULL COMMENT '所属排障会话ID',
    `source` VARCHAR(32) NOT NULL COMMENT 'es_log / codebase / mysql_db / git_history / runbook',
    `tool_name` VARCHAR(64) NOT NULL COMMENT '采集该证据的工具名',
    `query_args` VARCHAR(1000) NOT NULL COMMENT '调用工具时的入参 JSON 字符串',
    `summary` VARCHAR(500) NOT NULL COMMENT '证据简短描述',
    `raw_snippet` MEDIUMTEXT NOT NULL COMMENT '经脱敏且限长后的真实返回内容',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`evidence_id`),
    KEY `idx_incident_ev` (`incident_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='工具层自动签发的真实证据白名单表';

-- 表 5: 代码补丁与沙箱自愈审批表
CREATE TABLE IF NOT EXISTS `patch_records` (
    `id` VARCHAR(64) NOT NULL COMMENT '补丁记录ID',
    `incident_id` VARCHAR(64) NOT NULL COMMENT '所属排障会话ID',
    `target_file` VARCHAR(500) NOT NULL COMMENT '被修改的源码文件相对路径',
    `patch_diff` MEDIUMTEXT NOT NULL COMMENT 'git diff 生成的标准代码修改对比文本',
    `test_command` VARCHAR(255) NOT NULL COMMENT '执行的单元测试命令',
    `test_passed` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '沙箱单测是否通过: 1/0',
    `test_output` MEDIUMTEXT NULL COMMENT 'pytest 测试控制台输出',
    `self_heal_retries` INT NOT NULL DEFAULT 0 COMMENT '沙箱内自愈重改次数 (0~3)',
    `approval_status` VARCHAR(32) NOT NULL DEFAULT 'pending' COMMENT 'pending / approved / rejected',
    `human_feedback` VARCHAR(1000) NULL COMMENT '人工驳回修改意见',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_incident_patch` (`incident_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代码修复补丁与沙箱单测自愈审批记录表';

-- 表 6: 历史故障复盘知识库主表
CREATE TABLE IF NOT EXISTS `knowledge_runbooks` (
    `case_id` VARCHAR(64) NOT NULL COMMENT '历史故障案例编号 (与ES和Qdrant同号)',
    `title` VARCHAR(255) NOT NULL COMMENT '故障案例标题',
    `error_signature` VARCHAR(255) NULL COMMENT '特征异常类名或报错码',
    `api_or_module` VARCHAR(255) NULL COMMENT '关联接口路由或模块文件名',
    `symptom_desc` TEXT NOT NULL COMMENT '故障表面症状描述',
    `root_cause` TEXT NOT NULL COMMENT '经验证的确凿根因',
    `resolution` TEXT NOT NULL COMMENT '标准修复方案',
    `source_incident_id` VARCHAR(64) NULL COMMENT '来源排障会话ID',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`case_id`),
    KEY `idx_error_sig` (`error_signature`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='历史故障复盘与运维手册知识库主表';


-- ============================================================================
-- 第二部分：被监控微商城故障靶场业务表 (供数据探针 Worker C 执行只读 NL2SQL)
-- ============================================================================
CREATE DATABASE IF NOT EXISTS `mall_target_db` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `mall_target_db`;

CREATE TABLE IF NOT EXISTS `mall_users` (
    `id` BIGINT NOT NULL PRIMARY KEY,
    `username` VARCHAR(64) NOT NULL,
    `vip_level` INT NOT NULL DEFAULT 0 COMMENT '0普通, 1白银, 2黄金会员, 3钻石会员',
    `status` VARCHAR(32) NOT NULL DEFAULT 'ACTIVE'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `mall_coupons` (
    `id` BIGINT NOT NULL PRIMARY KEY,
    `user_id` BIGINT NOT NULL,
    `coupon_type` VARCHAR(32) NOT NULL COMMENT 'FULL_REDUCE(满减券) / RATE(折扣券)',
    `discount_amount` DECIMAL(10, 2) NULL COMMENT '固定减免金额 (当 coupon_type=RATE 时此字段为 NULL)',
    `discount_rate` DECIMAL(4, 2) NULL COMMENT '折扣率，如 0.80 表示8折',
    `status` VARCHAR(32) NOT NULL DEFAULT 'UNUSED'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `mall_orders` (
    `id` BIGINT NOT NULL PRIMARY KEY,
    `user_id` BIGINT NOT NULL,
    `coupon_id` BIGINT NULL,
    `total_price` DECIMAL(10, 2) NOT NULL,
    `final_price` DECIMAL(10, 2) NOT NULL,
    `order_status` VARCHAR(32) NOT NULL,
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 预置剧本一与剧本二的靶场测试数据
INSERT IGNORE INTO `mall_users` (`id`, `username`, `vip_level`, `status`) VALUES
(10086, 'chen_vip_user', 2, 'ACTIVE'),
(10087, 'normal_buyer', 0, 'ACTIVE');

INSERT IGNORE INTO `mall_coupons` (`id`, `user_id`, `coupon_type`, `discount_amount`, `discount_rate`, `status`) VALUES
(501, 10086, 'RATE', NULL, 0.80, 'UNUSED'),
(502, 10087, 'FULL_REDUCE', 20.00, NULL, 'UNUSED');
