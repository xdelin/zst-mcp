// 定时任务调度器：每 30s 扫描所有用户的 tasks.json，到点触发 Dify 运行（综合 app）
// 任务定义存于 users/<u>/tasks.json：[{id,name,schedule,prompt,enabled,conversation_id,last_run,last_status}]
import fs from "fs";
import path from "path";
import axios from "axios";
import {
  USERS_DIR,
  readJson,
  writeJson,
  appendJsonl,
  cronMatches,
  nowIso,
  validUser,
} from "./store.js";

const DIFY_BASE_URL = () => process.env.DIFY_BASE_URL || "http://<internal-host>/v1";
const DIFY_APP_TOKEN = () => process.env.DIFY_APP_TOKEN || ""; // 综合 app 的 API token（app-…）；延迟读取（import 早于 dotenv.config）
const RUN_TIMEOUT_MS = () => Number(process.env.TASK_RUN_TIMEOUT_MS || 900000);

let lastTickMinute = null;

async function runTask(user, task) {
  const query = `【定时任务】${task.name}\n${task.prompt}`;
  const startedAt = nowIso();
  let status = "ok";
  let answerHead = "";
  try {
    const resp = await axios.post(
      `${DIFY_BASE_URL()}/chat-messages`,
      {
        inputs: {},
        query,
        response_mode: "streaming",
        user,
        conversation_id: task.conversation_id || "",
        auto_generate_name: true,
      },
      {
        headers: { Authorization: `Bearer ${DIFY_APP_TOKEN()}` },
        timeout: RUN_TIMEOUT_MS(),
        responseType: "stream",
      }
    );
    // 消费 SSE 流到结束；agent 应用正文在 message 事件携带完整内容
    await new Promise((resolve, reject) => {
      let buf = "";
      resp.data.on("data", (chunk) => {
        buf += chunk.toString();
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const dataLine = block.split("\n").find((l) => l.startsWith("data:"));
          if (!dataLine) continue;
          try {
            const evt = JSON.parse(dataLine.slice(5));
            if (evt.event === "message" && evt.answer) answerHead = (answerHead + evt.answer).slice(0, 2000);
            if (evt.event === "error") status = `error: ${evt.message || "unknown"}`;
          } catch {
            /* 忽略非 JSON 块 */
          }
        }
      });
      resp.data.on("end", resolve);
      resp.data.on("error", reject);
    });
  } catch (e) {
    status = `error: ${e.message}`;
  }
  const record = {
    type: "scheduled_run",
    task_id: task.id,
    task_name: task.name,
    user,
    started_at: startedAt,
    finished_at: nowIso(),
    status,
    answer_head: answerHead.slice(0, 500),
  };
  appendJsonl(path.join(USERS_DIR, user, "task-history.jsonl"), record);
  console.log(`[scheduler] run ${user}/${task.name} -> ${status}`);
  return status;
}

function tick() {
  const now = new Date();
  const minuteKey = `${now.getFullYear()}-${now.getMonth() + 1}-${now.getDate()}-${now.getHours()}-${now.getMinutes()}`;
  if (minuteKey === lastTickMinute) return; // 每分钟只判一次
  lastTickMinute = minuteKey;

  if (!fs.existsSync(USERS_DIR)) return;
  for (const user of fs.readdirSync(USERS_DIR, { withFileTypes: true })) {
    if (!user.isDirectory() || !validUser(user.name)) continue;
    const tasksFile = path.join(USERS_DIR, user.name, "tasks.json");
    const tasks = readJson(tasksFile, []);
    if (!Array.isArray(tasks)) continue;
    let changed = false;
    for (const task of tasks) {
      if (!task || task.enabled === false) continue;
      if (task.last_run && task.last_run.slice(0, 16) === now.toISOString().slice(0, 16)) continue;
      if (!cronMatches(task.schedule, now)) continue;
      task.last_run = nowIso();
      task.last_status = "running";
      changed = true;
      // 异步执行，不阻塞扫描
      runTask(user.name, task)
        .then((status) => {
          task.last_status = status;
          writeJson(tasksFile, tasks);
        })
        .catch((e) => {
          console.error(`[scheduler] runTask failed ${user.name}/${task.name}:`, e.message);
          task.last_status = `error: ${e.message}`;
          writeJson(tasksFile, tasks);
        });
    }
    if (changed) writeJson(tasksFile, tasks);
  }
}

export function startScheduler() {
  if (!DIFY_APP_TOKEN()) {
    console.warn("[scheduler] DIFY_APP_TOKEN 未配置，定时任务调度器未启用");
    return;
  }
  setInterval(tick, 30 * 1000);
  console.log("[scheduler] 定时任务调度器已启动（每分钟检查一次）");
}
