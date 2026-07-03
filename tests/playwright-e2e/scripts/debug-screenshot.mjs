import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";

const BASE_URL = process.env.BASE_URL ?? "http://localhost:5173";
const OUTPUT_DIR = "/Users/zata/code/zata_code_template/tests/playwright-e2e/screenshots";

const SESSION = { user_id: "u_demo_001", display_name: "陈砚", email: "chen.yan@my-app.dev" };

async function main() {
  await mkdir(OUTPUT_DIR, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
  });
  await context.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const url = route.request().url();
    console.log(`[mock] ${url}`);
    if (url.endsWith("/api/auth/me")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SESSION) });
      return;
    }
    await route.fulfill({ status: 204, body: "" });
  });
  const page = await context.newPage();
  page.on("console", (msg) => console.log(`[browser:${msg.type()}] ${msg.text()}`));
  page.on("pageerror", (err) => console.log(`[pageerror] ${err.message}`));
  await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(4000);
  console.log("--- URL:", page.url());
  console.log("--- TITLE:", await page.title());
  const text = await page.evaluate(() => document.body.innerText.slice(0, 800));
  console.log("--- TEXT:", text);
  await page.screenshot({ path: join(OUTPUT_DIR, "dashboard-debug.png"), fullPage: true });
  await browser.close();
}
main().catch((err) => { console.error(err); process.exit(1); });
