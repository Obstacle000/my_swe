1. 去掉测试用repo,及其关联的依赖,初始化数据逻辑,以及图里里面的硬编码,比如run_pytest_node
2. 搞个外部工具server,比如mcp,得搞oAuth(或者PAT)
3. 把log_es删了,引入Filebeat(学),日志管理
4. 观测源协议标准化与多数据源适配器 (DataSource Adapter / OpenTelemetry)
5. 统一排障上下文与配置注入 (Incident Context & Tenant Isolation)
6. 工具权限与安全沙箱控制 (Tool Guardrails & Read/Write Separation)
7. CI/CD 与真实发布流水线对接 (GitOps / Webhook 回环)
8. 知识库/Runbook 的动态外置 (从硬编码初始化 -> 真实运维文档系统对接)
9. git tree沙箱
10. PROJECT_ROOT智能识别
11. SLACK_API通知
12. 增加探针,比如git提交记录