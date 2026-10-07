// usermem-mcp：用户级跨会话记忆层
// - MCP（SSE /sse + Streamable HTTP /mcp）→ Dify agent 工具调用
// - REST /api/*（X-Usermem-Key 鉴权）→ zhisiti 服务端（技能面板）
// - 调度器 → 定时任务按 cron 触发 Dify 运行
// 存储：data/users/<cas用户名>/{preferences.json, skills.json, files/, tasks.json, task-history.jsonl, deliverables/}
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { SSEServerTransport } from "@modelcontextprotocol/sdk/server/sse.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { ListToolsRequestSchema, CallToolRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import express from "express";
import dotenv from "dotenv";
dotenv.config();
import cors from "cors";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import {
  USERS_DIR,
  userDir,
  validUser,
  safeUserFilePath,
  readJson,
  writeJson,
  appendJsonl,
  isValidCron,
  nowIso,
} from "./store.js";
import { ensureCloudSkillsImported, getSkillCatalog } from "./catalog.js";
import { startScheduler } from "./scheduler.js";

const PORT = Number(process.env.PORT || 8083);
const USERMEM_KEY = process.env.USERMEM_KEY || "";
const MAX_FILE_BYTES = 64 * 1024;

// ---------- MCP 工具定义 ----------
// user 参数为 CAS 用户名（Dify 侧系统提示词注入 [用户上下文] 用户标识:<id>）
const TOOLS = [
  {
    name: "write_preference",
    description:
      "静默记录用户的长期偏好（跨会话保留，供所有助手读取）。何时调用：用户表达稳定的个人特征或要求时，如“以后都用中文回复”“学习时要举例子”“我喜欢简洁的答案”。仅在偏好稳定可复用时调用一次，不要重复记录同类偏好，也不要向用户复述你记录了什么。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识，来自系统提示 [用户上下文] 中的用户标识" },
        content: { type: "string", description: "偏好内容，一句话，如：回复使用中文，讲解时多举例子" },
      },
      required: ["user", "content"],
    },
  },
  {
    name: "read_preferences",
    description:
      "读取用户的长期偏好列表（跨会话保留）。开始回答前调用一次，并在回答中自然遵循这些偏好，无需向用户提及读取过程。",
    inputSchema: {
      type: "object",
      properties: { user: { type: "string", description: "用户标识" } },
      required: ["user"],
    },
  },
  {
    name: "get_user_overview",
    description:
      "读取用户级跨会话记忆全貌：长期偏好、已安装技能、用户级文件（账号绑定/登录态等）、定时任务、近期任务历史。开始新任务时调用一次以了解用户背景。",
    inputSchema: {
      type: "object",
      properties: { user: { type: "string", description: "用户标识" } },
      required: ["user"],
    },
  },
  {
    name: "save_user_file",
    description:
      "保存一个用户级小文本文件（≤64KB，跨会话保留）。用于存放账号绑定、登录态、用户笔记等需要跨对话复用的数据，如 bindings/wecom.json。不要存大文件。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识" },
        path: { type: "string", description: "相对路径，如 bindings/wecom.json" },
        content: { type: "string", description: "文件内容（文本）" },
      },
      required: ["user", "path", "content"],
    },
  },
  {
    name: "read_user_file",
    description: "读取一个用户级文件的内容（跨会话保留），如之前保存的账号绑定或登录态。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识" },
        path: { type: "string", description: "相对路径" },
      },
      required: ["user", "path"],
    },
  },
  {
    name: "list_user_files",
    description: "列出用户级文件（跨会话保留的账号绑定/登录态/笔记等）。",
    inputSchema: {
      type: "object",
      properties: { user: { type: "string", description: "用户标识" } },
      required: ["user"],
    },
  },
  {
    name: "record_task",
    description:
      "任务结束时记录一条任务历史（标题+摘要），跨会话保留，供以后回顾用户做过什么。每个完成的任务记一条即可。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识" },
        title: { type: "string", description: "任务标题，如：芯片存储行业早报" },
        summary: { type: "string", description: "一两句任务结果摘要" },
      },
      required: ["user", "title", "summary"],
    },
  },
  {
    name: "list_task_history",
    description: "列出用户的近期任务历史（跨会话保留）。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识" },
        limit: { type: "number", description: "返回条数，默认 20" },
      },
      required: ["user"],
    },
  },
  {
    name: "create_scheduled_task",
    description:
      "创建定时任务（在云端按计划自动运行，与创建它的对话无关）。schedule 为 5 字段 cron（分 时 日 月 周），如“0 8 * * *”表示每天 8:00。任务将按用户的身份自动运行 prompt 描述的工作。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识" },
        name: { type: "string", description: "任务名，如：芯片存储每日早报" },
        schedule: { type: "string", description: "5 字段 cron 表达式（分 时 日 月 周）" },
        prompt: { type: "string", description: "任务运行时要执行的工作描述（会作为你的指令执行）" },
      },
      required: ["user", "name", "schedule", "prompt"],
    },
  },
  {
    name: "list_scheduled_tasks",
    description: "列出用户的所有定时任务（含启用状态与最近运行状态）。",
    inputSchema: {
      type: "object",
      properties: { user: { type: "string", description: "用户标识" } },
      required: ["user"],
    },
  },
  {
    name: "update_scheduled_task",
    description: "修改用户的定时任务（可改名称、cron、prompt、启用状态）。按 id 定位。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识" },
        id: { type: "string", description: "任务 id" },
        name: { type: "string", description: "新名称（可选）" },
        schedule: { type: "string", description: "新 cron（可选）" },
        prompt: { type: "string", description: "新工作描述（可选）" },
        enabled: { type: "boolean", description: "启用/停用（可选）" },
      },
      required: ["user", "id"],
    },
  },
  {
    name: "delete_scheduled_task",
    description: "删除用户的定时任务。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识" },
        id: { type: "string", description: "任务 id" },
      },
      required: ["user", "id"],
    },
  },
  {
    name: "record_deliverable",
    description:
      "登记一个交付文件到用户的交付索引（按对话归档，文件本身持久保留；新对话默认不指向旧对话目录）。ref 为文件的引用（如 Dify 文件 id 或 URL）。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识" },
        conversation_id: { type: "string", description: "对话 id，来自系统提示 [用户上下文] 中的会话" },
        name: { type: "string", description: "文件名，如 行业早报.pptx" },
        ref: { type: "string", description: "文件引用（Dify 文件 id 或 URL）" },
      },
      required: ["user", "conversation_id", "name", "ref"],
    },
  },
  {
    name: "list_deliverables",
    description: "列出用户的交付文件索引（可按对话过滤）。",
    inputSchema: {
      type: "object",
      properties: {
        user: { type: "string", description: "用户标识" },
        conversation_id: { type: "string", description: "对话 id（可选，不过滤则列出全部）" },
      },
      required: ["user"],
    },
  },
];

// ---------- 工具实现 ----------
function err(msg) {
  return { content: [{ type: "text", text: `❌ ${msg}` }] };
}
function ok(text) {
  return { content: [{ type: "text", text }] };
}

function prefsFile(user) {
  return path.join(userDir(user), "preferences.json");
}
function tasksFile(user) {
  return path.join(userDir(user), "tasks.json");
}
function skillsFile(user) {
  return path.join(userDir(user), "skills.json");
}
function historyFile(user) {
  return path.join(userDir(user), "task-history.jsonl");
}
function deliverablesFile(user) {
  return path.join(userDir(user), "deliverables.json");
}
function settingsFile(user) {
  return path.join(userDir(user), "settings.json");
}

// 记忆总开关（用户级，默认开）。关闭后所有记忆类 MCP 工具返回「已关闭」，
// agent 侧提示词据此不再依赖记忆；数据保留在磁盘，重新开启即恢复。
function memoryEnabled(user) {
  if (!validUser(user)) return true;
  return readJson(settingsFile(user), {}).memory_enabled !== false;
}
const MEMORY_DISABLED_MSG = "用户已关闭偏好记忆，本轮不要读写任何用户级记忆数据。";

function listFilesRecursive(base, rel = "") {
  const out = [];
  const full = path.join(base, rel);
  if (!fs.existsSync(full)) return out;
  for (const ent of fs.readdirSync(full, { withFileTypes: true })) {
    const relPath = rel ? `${rel}/${ent.name}` : ent.name;
    if (ent.isDirectory()) out.push(...listFilesRecursive(base, relPath));
    else out.push(relPath);
  }
  return out;
}

function readHistory(user, limit = 20) {
  const file = historyFile(user);
  if (!fs.existsSync(file)) return [];
  const lines = fs.readFileSync(file, "utf8").split("\n").filter(Boolean);
  return lines.slice(-limit).map((l) => {
    try {
      return JSON.parse(l);
    } catch {
      return { raw: l };
    }
  });
}

const HANDLERS = {
  write_preference(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user) || !args.content?.trim()) return err("user 或 content 无效");
    const file = prefsFile(args.user);
    const prefs = readJson(file, []);
    prefs.push({ content: args.content.trim(), created_at: nowIso() });
    writeJson(file, prefs);
    return ok("已记录用户偏好（静默）。");
  },
  read_preferences(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user)) return err("user 无效");
    const prefs = readJson(prefsFile(args.user), []);
    if (!prefs.length) return ok("该用户暂无记录的偏好。");
    return ok(`用户偏好（跨会话保留，回答时自然遵循）：\n` + prefs.map((p, i) => `${i + 1}. ${p.content}`).join("\n"));
  },
  get_user_overview(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user)) return err("user 无效");
    const prefs = readJson(prefsFile(args.user), []);
    const skills = readJson(skillsFile(args.user), []);
    const files = listFilesRecursive(path.join(userDir(args.user), "files"));
    const tasks = readJson(tasksFile(args.user), []);
    const history = readHistory(args.user, 10).reverse();
    const deliverables = readJson(deliverablesFile(args.user), {});
    const parts = [];
    parts.push(
      `## 用户偏好\n` + (prefs.length ? prefs.map((p) => `- ${p.content}`).join("\n") : "（暂无）")
    );
    parts.push(
      `## 已安装技能\n` +
        (skills.length ? skills.map((s) => `- ${s.name}：${s.display_name || ""}`).join("\n") : "（无）")
    );
    parts.push(
      `## 用户级文件（账号绑定/登录态/笔记）\n` + (files.length ? files.map((f) => `- ${f}`).join("\n") : "（无）")
    );
    parts.push(
      `## 定时任务\n` +
        (tasks.length
          ? tasks
              .map((t) => `- [${t.enabled === false ? "停用" : "启用"}] ${t.name}（${t.schedule}）最近状态：${t.last_status || "未运行"}`)
              .join("\n")
          : "（无）")
    );
    const convIds = Object.keys(deliverables);
    parts.push(
      `## 交付文件索引\n` +
        (convIds.length
          ? convIds
              .map((c) => `- 会话 ${c.slice(0, 8)}…：${deliverables[c].map((d) => d.name).join("、")}`)
              .join("\n")
          : "（无）")
    );
    parts.push(
      `## 近期任务历史\n` +
        (history.length
          ? history.map((h) => `- ${h.title || h.task_name || "?"}：${h.summary || h.status || ""}`).join("\n")
          : "（无）")
    );
    return ok(`用户级跨会话记忆（用户 ${args.user}）：\n\n` + parts.join("\n\n"));
  },
  save_user_file(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    const full = safeUserFilePath(args.user, args.path);
    if (!full) return err("user 或 path 无效（不允许绝对路径或 ..）");
    if (Buffer.byteLength(args.content || "") > MAX_FILE_BYTES) return err("文件过大（上限 64KB），请只存文本类数据");
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, args.content);
    return ok(`已保存用户级文件 ${args.path}（跨会话保留）。`);
  },
  read_user_file(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    const full = safeUserFilePath(args.user, args.path);
    if (!full || !fs.existsSync(full)) return err("文件不存在或路径无效");
    const content = fs.readFileSync(full, "utf8");
    if (content.length > 10000) return ok(content.slice(0, 10000) + "\n…（已截断）");
    return ok(content);
  },
  list_user_files(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user)) return err("user 无效");
    const files = listFilesRecursive(path.join(userDir(args.user), "files"));
    return ok(files.length ? `用户级文件：\n` + files.map((f) => `- ${f}`).join("\n") : "（无用户级文件）");
  },
  record_task(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user) || !args.title?.trim()) return err("user 或 title 无效");
    appendJsonl(historyFile(args.user), {
      type: "task",
      title: args.title.trim(),
      summary: (args.summary || "").slice(0, 2000),
      at: nowIso(),
    });
    return ok("已记录任务历史。");
  },
  list_task_history(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user)) return err("user 无效");
    const items = readHistory(args.user, Math.min(Number(args.limit) || 20, 100)).reverse();
    return ok(
      items.length
        ? `任务历史（新→旧）：\n` + items.map((h) => `- ${h.at || ""} ${h.title || h.task_name || "?"}：${h.summary || h.status || ""}`).join("\n")
        : "（无任务历史）"
    );
  },
  create_scheduled_task(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user)) return err("user 无效");
    if (!args.name?.trim() || !args.prompt?.trim()) return err("name 或 prompt 无效");
    if (!isValidCron(args.schedule)) return err("schedule 必须是 5 字段 cron（分 时 日 月 周），如 0 8 * * *");
    const file = tasksFile(args.user);
    const tasks = readJson(file, []);
    const task = {
      id: crypto.randomUUID(),
      name: args.name.trim(),
      schedule: args.schedule.trim(),
      prompt: args.prompt.trim(),
      enabled: true,
      created_at: nowIso(),
    };
    tasks.push(task);
    writeJson(file, tasks);
    return ok(`已创建定时任务「${task.name}」（${task.schedule}），将按计划以该用户身份自动运行。id: ${task.id}`);
  },
  list_scheduled_tasks(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user)) return err("user 无效");
    const tasks = readJson(tasksFile(args.user), []);
    return ok(
      tasks.length
        ? tasks
            .map(
              (t) =>
                `- ${t.name}（${t.schedule}，${t.enabled === false ? "停用" : "启用"}，最近：${t.last_status || "未运行"}）id: ${t.id}`
            )
            .join("\n")
        : "（无定时任务）"
    );
  },
  update_scheduled_task(args) {
    if (!validUser(args.user)) return err("user 无效");
    const file = tasksFile(args.user);
    const tasks = readJson(file, []);
    const task = tasks.find((t) => t.id === args.id);
    if (!task) return err("未找到该任务 id");
    if (args.name !== undefined) task.name = String(args.name).trim();
    if (args.schedule !== undefined) {
      if (!isValidCron(args.schedule)) return err("schedule 必须是 5 字段 cron");
      task.schedule = args.schedule.trim();
    }
    if (args.prompt !== undefined) task.prompt = String(args.prompt).trim();
    if (args.enabled !== undefined) task.enabled = Boolean(args.enabled);
    task.updated_at = nowIso();
    writeJson(file, tasks);
    return ok(`已更新定时任务「${task.name}」。`);
  },
  delete_scheduled_task(args) {
    if (!validUser(args.user)) return err("user 无效");
    const file = tasksFile(args.user);
    const tasks = readJson(file, []);
    const idx = tasks.findIndex((t) => t.id === args.id);
    if (idx < 0) return err("未找到该任务 id");
    const [removed] = tasks.splice(idx, 1);
    writeJson(file, tasks);
    return ok(`已删除定时任务「${removed.name}」。`);
  },
  record_deliverable(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user) || !args.name?.trim() || !args.conversation_id) return err("参数无效");
    const file = deliverablesFile(args.user);
    const all = readJson(file, {});
    const conv = args.conversation_id.slice(0, 64);
    (all[conv] = all[conv] || []).push({ name: args.name.trim(), ref: (args.ref || "").slice(0, 500), at: nowIso() });
    writeJson(file, all);
    return ok(`已登记交付文件「${args.name}」到会话 ${conv.slice(0, 8)}…`);
  },
  list_deliverables(args) {
    if (!memoryEnabled(args.user)) return ok(MEMORY_DISABLED_MSG);
    if (!validUser(args.user)) return err("user 无效");
    const all = readJson(deliverablesFile(args.user), {});
    const convs = args.conversation_id ? [args.conversation_id.slice(0, 64)] : Object.keys(all);
    const lines = convs.filter((c) => all[c]?.length).map((c) => {
      const date = all[c][0].at?.slice(0, 10) || "";
      return `- 会话 ${c.slice(0, 8)}…（${date}）：` + all[c].map((d) => d.name).join("、");
    });
    return ok(lines.length ? `交付文件索引：\n` + lines.join("\n") : "（无交付文件）");
  },
};

// ---------- MCP Server ----------
function createMcpServer() {
  const server = new Server(
    { name: "usermem-mcp", version: "1.0.0" },
    { capabilities: { tools: {} } }
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args = {} } = request.params;
    const handler = HANDLERS[name];
    if (!handler) return err(`未知工具：${name}`);
    console.log(`[mcp] ${name} user=${args.user || "-"}`);
    try {
      return handler(args);
    } catch (e) {
      console.error(`[mcp] ${name} failed:`, e);
      return err(`执行失败：${e.message}`);
    }
  });

  return server;
}

// ---------- Express ----------
const app = express();
app.use(cors());
app.use(express.json({ limit: "1mb" }));

// REST：供 zhisiti 服务端（共享密钥）
function requireKey(req, res, next) {
  if (!USERMEM_KEY) return res.status(500).json({ error: "USERMEM_KEY 未配置" });
  if (req.get("X-Usermem-Key") !== USERMEM_KEY) return res.status(401).json({ error: "unauthorized" });
  next();
}

app.get("/api/health", (req, res) => res.json({ ok: true }));

app.get("/api/catalog", requireKey, async (req, res) => {
  try {
    res.set("Cache-Control", "no-store").json({ catalog: await getSkillCatalog() });
  } catch (e) {
    console.error("[api] catalog failed:", e.message);
    res.status(500).json({ error: e.message });
  }
});

app.get("/api/users/:user/skills", requireKey, async (req, res) => {
  if (!validUser(req.params.user)) return res.status(400).json({ error: "invalid user" });
  try {
    const skills = readJson(skillsFile(req.params.user), []);
    const catalog = new Map((await getSkillCatalog()).map((skill) => [skill.name, skill]));
    res.set("Cache-Control", "no-store").json({
      skills: skills.map((skill) => ({
        ...skill,
        display_name: catalog.get(skill.name)?.display_name || skill.display_name,
      })),
    });
  } catch (e) {
    console.error("[api] installed skills failed:", e.message);
    res.status(500).json({ error: "技能列表读取失败" });
  }
});

// 用户设置（zhisiti 设置面板用）：memory_enabled 是记忆总开关
app.get("/api/users/:user/settings", requireKey, (req, res) => {
  if (!validUser(req.params.user)) return res.status(400).json({ error: "invalid user" });
  const settings = readJson(settingsFile(req.params.user), {});
  res.json({ settings: { memory_enabled: settings.memory_enabled !== false } });
});

app.put("/api/users/:user/settings", requireKey, (req, res) => {
  const user = req.params.user;
  if (!validUser(user)) return res.status(400).json({ error: "invalid user" });
  if (!req.body || typeof req.body !== "object" || typeof req.body.memory_enabled !== "boolean") {
    return res.status(400).json({ error: "memory_enabled(boolean) 必填" });
  }
  const settings = readJson(settingsFile(user), {});
  settings.memory_enabled = req.body.memory_enabled;
  settings.updated_at = nowIso();
  writeJson(settingsFile(user), settings);
  res.json({ ok: true, settings: { memory_enabled: settings.memory_enabled } });
});

app.put("/api/users/:user/skills", requireKey, async (req, res) => {
  const user = req.params.user;
  if (!validUser(user)) return res.status(400).json({ error: "invalid user" });
  const skills = Array.isArray(req.body.skills) ? req.body.skills : null;
  if (!skills) return res.status(400).json({ error: "skills 数组必填" });
  if (skills.length > 200) return res.status(400).json({ error: "skills 过多" });
  for (const s of skills) {
    // 上限 80：cloud-skills 有个别 id 超 64（导入脚本会自动规范到 Dify 的 64 上限）
    if (typeof s?.name !== "string" || !/^[A-Za-z0-9._-]{1,80}$/.test(s.name)) {
      return res.status(400).json({ error: `非法技能名：${s?.name}` });
    }
  }
  // 懒导入：仅对本次新增的 cloud 技能，写入 Dify 并绑定综合 agent（幂等）。
  // 等待完成再响应——试用流程中用户点发送前导入已就绪；容器内脚本约 15-20s。
  const prevNames = new Set(readJson(skillsFile(user), []).map((s) => s.name));
  const newNames = skills.filter((s) => !prevNames.has(s.name)).map((s) => s.name);
  if (newNames.length > 0) await ensureCloudSkillsImported(newNames);
  writeJson(skillsFile(user), skills.map((s) => ({ name: s.name, display_name: String(s.display_name || s.name).slice(0, 128), icon: String(s.icon || "📄").slice(0, 8), installed_at: s.installed_at || nowIso() })));
  res.json({ ok: true, skills: readJson(skillsFile(user), []) });
});

// MCP：Legacy SSE
const transports = {};
app.get("/sse", async (req, res) => {
  const transport = new SSEServerTransport("/messages", res);
  const serverInstance = createMcpServer();
  transports[transport.sessionId] = transport;
  res.on("close", () => delete transports[transport.sessionId]);
  await serverInstance.connect(transport);
});
app.post("/messages", async (req, res) => {
  const transport = transports[req.query.sessionId];
  if (!transport) return res.status(400).send("No transport found for sessionId.");
  await transport.handlePostMessage(req, res, req.body);
});

// MCP：Streamable HTTP
const streamableTransports = new Map();
app.all("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"];
  let transport;
  if (sessionId && streamableTransports.has(sessionId)) {
    transport = streamableTransports.get(sessionId);
  } else {
    transport = new StreamableHTTPServerTransport({ sessionIdGenerator: () => crypto.randomUUID() });
    const serverInstance = createMcpServer();
    serverInstance.connect(transport);
    transport.onclose = () => {
      if (transport.sessionId) streamableTransports.delete(transport.sessionId);
    };
  }
  await transport.handleRequest(req, res, req.body);
  if (transport.sessionId && !streamableTransports.has(transport.sessionId)) {
    streamableTransports.set(transport.sessionId, transport);
  }
});

app.get("/", (req, res) => {
  res.send("usermem-mcp running. MCP: /sse /mcp；REST: /api/*（X-Usermem-Key）");
});

fs.mkdirSync(USERS_DIR, { recursive: true });
app.listen(PORT, "0.0.0.0", () => {
  console.log(`✅ usermem-mcp listening on :${PORT}`);
  console.log(`   MCP SSE: /sse   Streamable HTTP: /mcp`);
  startScheduler();
});
