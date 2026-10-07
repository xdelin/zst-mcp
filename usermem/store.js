// 常量与共享工具：路径安全、JSON 读写、cron 匹配
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, "data");
export const USERS_DIR = path.join(DATA_DIR, "users");

// CAS 用户名（学号等）：字母数字与 ._-，1-64 位
const USER_RE = /^[A-Za-z0-9._-]{1,64}$/;
// 用户级文件相对路径：不允许 .. 与绝对路径
const REL_RE = /^[A-Za-z0-9._\-/]{1,200}$/;

export function validUser(user) {
  return typeof user === "string" && USER_RE.test(user);
}

export function userDir(user) {
  return path.join(USERS_DIR, user);
}

export function safeUserFilePath(user, relPath) {
  if (!validUser(user) || typeof relPath !== "string" || !REL_RE.test(relPath)) return null;
  const base = path.join(userDir(user), "files");
  const full = path.resolve(base, relPath);
  if (full !== base && !full.startsWith(base + path.sep)) return null;
  return full;
}

export function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

export function writeJson(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = file + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  fs.renameSync(tmp, file);
}

export function appendJsonl(file, obj) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, JSON.stringify(obj) + "\n");
}

// ---------- 极简 5 字段 cron 匹配（分 时 日 月 周）----------
// 支持 *、*/n、a-b、a,b,c、以及它们的组合
function matchField(field, value) {
  for (const part of field.split(",")) {
    const m = part.match(/^(\*|\d+(?:-\d+)?)(?:\/(\d+))?$/);
    if (!m) continue;
    let [lo, hi] = [null, null];
    if (m[1] === "*") {
      [lo, hi] = [0, 1e9];
    } else if (m[1].includes("-")) {
      [lo, hi] = m[1].split("-").map(Number);
    } else {
      lo = hi = Number(m[1]);
    }
    const step = m[2] ? Number(m[2]) : 1;
    if (value < lo) continue;
    if (hi < 1e9 && value > hi) continue;
    if ((value - lo) % step === 0) return true;
  }
  return false;
}

// cron 5 字段；date 为本地时间。周字段：0/7=周日，1-6=周一到周六
export function cronMatches(cron, date) {
  const fields = String(cron).trim().split(/\s+/);
  if (fields.length !== 5) return false;
  const [mi, h, dom, mon, dow] = fields;
  const dayOfWeek = date.getDay();
  const domRestricted = dom !== "*" && !dom.startsWith("*/");
  const dowRestricted = dow !== "*" && !dow.startsWith("*/");
  // POSIX 语义：日与周都受限时，任一命中即触发；否则须都命中
  let dayOk;
  if (domRestricted && dowRestricted) {
    dayOk = matchField(dom, date.getDate()) || matchField(dow, dayOfWeek === 0 ? 7 : dayOfWeek);
  } else {
    dayOk = matchField(dom, date.getDate()) && matchField(dow, dayOfWeek);
  }
  return (
    matchField(mi, date.getMinutes()) &&
    matchField(h, date.getHours()) &&
    dayOk &&
    matchField(mon, date.getMonth() + 1)
  );
}

export function isValidCron(cron) {
  const fields = String(cron || "").trim().split(/\s+/);
  if (fields.length !== 5) return false;
  return fields.every((f) => /^[*0-9,\-/]+$/.test(f));
}

export function nowIso() {
  return new Date().toISOString();
}
