-- failure-mode 打标：messages 表 status='error' 的 69 条记录
-- 分类依据：dify-mods 9 个 patch + 平台故障模式；仅统计元数据，不触碰对话内容
SELECT
  CASE
    WHEN error LIKE '%file type parameter%' THEN 'multimodal_file_type'
    WHEN error LIKE '%"code":0,"message":"success"%' THEN 'run_silent_death'
    WHEN error LIKE '%Agent backend HTTP 429%' THEN 'rate_limit_triggered'
    WHEN error LIKE '%tool parameter%' OR error LIKE '%missing runtime parameters%' OR error LIKE '%Query is required%' THEN 'tool_config_drift'
    WHEN error LIKE '%killed by timeout%' OR error LIKE '%PluginDaemonInternalServerError%' OR error LIKE '%Plugin Daemon Service failed%' THEN 'plugin_daemon_error'
    WHEN error LIKE '%Failed to transform agent message%' THEN 'mcp_plugin_invoke_error'
    WHEN error LIKE '%bootstrap%' THEN 'bootstrap_failure'
    WHEN error LIKE '%validation errors%' OR error LIKE '%AgentNodeData%' THEN 'config_validation'
    WHEN error LIKE '%Exceeded maximum output retries%' THEN 'agent_retry_exhausted'
    WHEN error LIKE '%Server Unavailable%' OR error LIKE '%Failed to invoke model%' OR error LIKE '%HTTPConnectionPool%' OR error LIKE '%status code%' OR error LIKE '%API request failed%' THEN 'model_upstream'
    ELSE 'unknown'
  END AS failure_mode,
  COUNT(*) AS cnt
FROM messages
WHERE status = 'error'
GROUP BY 1
ORDER BY 2 DESC;
