import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";

const OUTPUT_DIR = join(process.cwd(), "screenshots");
const BASE_URL = process.env.BASE_URL ?? "http://localhost:5180";

const PUBLIC_TARGETS = [
  { name: "login-light-desktop", path: "/login", colorScheme: "light", viewport: { width: 1280, height: 800 } },
  { name: "login-dark-desktop", path: "/login", colorScheme: "dark", viewport: { width: 1280, height: 800 } },
  { name: "login-light-mobile", path: "/login", colorScheme: "light", viewport: { width: 390, height: 844 } },
  { name: "login-dark-mobile", path: "/login", colorScheme: "dark", viewport: { width: 390, height: 844 } },
];


const MOCKED_SESSION = {
  user_id: "u_demo_001",
  display_name: "陈砚",
  email: "chen.yan@my-app.dev",
};

const AUTH_TARGETS = [
  {
    name: "dashboard-light-desktop",
    path: "/dashboard",
    colorScheme: "light",
    viewport: { width: 1440, height: 900 },
  },
  {
    name: "dashboard-dark-desktop",
    path: "/dashboard",
    colorScheme: "dark",
    viewport: { width: 1440, height: 900 },
  },
  {
    name: "notfound-light-desktop",
    path: "/this-page-does-not-exist",
    colorScheme: "light",
    viewport: { width: 1440, height: 900 },
  },
  {
    name: "notfound-dark-desktop",
    path: "/this-page-does-not-exist",
    colorScheme: "dark",
    viewport: { width: 1440, height: 900 },
  },
];

async function setupAuthMock(context) {
  // Mock the session restore endpoint so RequireSession lets /dashboard
  // through. Anything else under /api returns 204 so failed network calls
  // don't surface as error toasts inside the screenshot.
  // Function predicate so we only intercept first-party API calls and not
  // Vite-served /shared/api/*.ts module URLs (which would otherwise get
  // 204/empty and break the JS module loader).
  await context.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    if (route.request().url().endsWith("/api/auth/me")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(MOCKED_SESSION),
      });
      return;
    }
    await route.fulfill({ status: 204, body: "" });
  });
}

async function main() {
  await mkdir(OUTPUT_DIR, { recursive: true });
  const browser = await chromium.launch();

  for (const target of PUBLIC_TARGETS) {
    const context = await browser.newContext({
      viewport: target.viewport,
      colorScheme: target.colorScheme,
      deviceScaleFactor: 1,
    });
    const page = await context.newPage();
    try {
      await page.goto(`${BASE_URL}${target.path}`, { waitUntil: "networkidle" });
      await page.waitForTimeout(800);
      const out = join(OUTPUT_DIR, `${target.name}.png`);
      await page.screenshot({ path: out, fullPage: true });
      console.log(`captured ${out}`);
    } catch (error) {
      console.error(`failed ${target.name}:`, error);
    } finally {
      await context.close();
    }
  }

  for (const target of AUTH_TARGETS) {
    const context = await browser.newContext({
      viewport: target.viewport,
      colorScheme: target.colorScheme,
      deviceScaleFactor: 1,
    });
    await setupAuthMock(context);
    const page = await context.newPage();
    try {
      await page.goto(`${BASE_URL}${target.path}`, { waitUntil: "networkidle" });
      await page.waitForTimeout(1500);
      const out = join(OUTPUT_DIR, `${target.name}.png`);
      await page.screenshot({ path: out, fullPage: true });
      console.log(`captured ${out}`);
    } catch (error) {
      console.error(`failed ${target.name}:`, error);
    } finally {
      await context.close();
    }
  }

  await browser.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
