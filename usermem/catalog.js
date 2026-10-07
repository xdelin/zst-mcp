import { execFile } from "child_process";
import fs from "fs";
import path from "path";

const CLOUD_SKILLS_DIR = process.env.CLOUD_SKILLS_DIR || "/home/<lab-user>/EduAgent/dify/cloud-skills";
const API_CONTAINER = process.env.DIFY_API_CONTAINER || "docker-api-1";
const IMPORT_SCRIPT = process.env.CLOUD_SKILL_IMPORT_SCRIPT || "/app/api/import_cloud_skill.py";

// 发布清单是唯一来源，不能用已导入的数据库技能补回下架项。
export async function getSkillCatalog() {
  const manifestPath = path.join(CLOUD_SKILLS_DIR, "manifest.json");
  const raw = JSON.parse(await fs.promises.readFile(manifestPath, "utf8"));
  return raw.skills.map((s) => ({
    name: s.id,
    display_name: s.name || s.id,
    icon: s.icon || "\u{1F4C4}",
    description: s.description || "",
    version: "1",
    primaryCategory: s.primaryCategory,
    secondaryCategory: s.secondaryCategory,
    author: s.author || "",
    downloads: s.downloads || "",
    stars: s.stars || "",
    source: "cloud",
  }));
}

async function getCloudNames() {
  return new Set((await getSkillCatalog()).map((skill) => skill.name));
}

// 懒导入：把 cloud 技能写入 Dify 并绑定到综合 agent（幂等；docker exec 容器内脚本）。
// 返回 Promise：调用方可等待（试用/安装场景确保用户发送前导入已就绪）；
// 失败不抛出（安装仍成功，仅 Dify 侧未就绪，下次安装任意技能时幂等重试）。
export function ensureCloudSkillsImported(names) {
  return new Promise((resolve) => {
    if (!Array.isArray(names) || names.length === 0) return resolve([]);
    getCloudNames()
      .then((cloudNames) => {
        const targets = [...new Set(names.filter((n) => typeof n === "string" && cloudNames.has(n)))];
        if (targets.length === 0) return resolve([]);
        const args = ["exec", "-e", "PYTHONPATH=/app/api", API_CONTAINER, "python3", IMPORT_SCRIPT];
        for (const t of targets) args.push("--skill", t);
        execFile("docker", args, { timeout: 120000 }, (err, stdout, stderr) => {
          if (err) {
            console.error(
              `[catalog] cloud skill import failed (${targets.join(",")}):`,
              err.message,
              String(stderr || "").slice(0, 200)
            );
          } else {
            // 脚本整体退出码为 0，逐技能状态在 stdout JSON 里
            try {
              const results = JSON.parse(String(stdout))?.results || [];
              const bad = results.filter((r) => r.status !== "imported" && r.status !== "already_imported");
              if (bad.length) {
                console.error("[catalog] cloud skill import errors:", JSON.stringify(bad));
              } else {
                console.log(`[catalog] cloud skill imported: ${targets.join(",")}`);
              }
            } catch {
              console.log(`[catalog] cloud skill import done (unparsed output): ${String(stdout).slice(0, 200)}`);
            }
          }
          resolve(targets);
        });
      })
      .catch(() => resolve([]));
  });
}
