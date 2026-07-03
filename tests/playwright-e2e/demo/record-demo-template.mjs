#!/usr/bin/env node
/**
 * Record a product demo video of the Zata Codes Template frontend.
 *
 * Run with a local frontend dev server (default Next.js port 3000):
 *   node tests/playwright-e2e/demo/record-demo-template.mjs
 *
 * To also demonstrate the authenticated area, start the backend and set:
 *   DEMO_IDENTIFIER=admin DEMO_PASSWORD=admin node tests/playwright-e2e/demo/record-demo-template.mjs
 *
 * The output webm is saved under tests/playwright-e2e/demo/videos/demo-template.webm.
 */
import { chromium } from '@playwright/test'
import { mkdir, rename, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const baseURL = process.env.DEMO_BASE_URL ?? 'http://127.0.0.1:3000'
const outputDirectory = resolve(__dirname, 'videos')

const credentials = {
  identifier: process.env.DEMO_IDENTIFIER ?? '',
  password: process.env.DEMO_PASSWORD ?? '',
}
const shouldLogin = credentials.identifier && credentials.password

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function narrate(page, message, durationMs = 1_500) {
  await page.evaluate((text) => {
    const existing = document.getElementById('demo-caption')
    if (existing) existing.remove()
    const el = document.createElement('div')
    el.id = 'demo-caption'
    el.textContent = text
    el.style.cssText = [
      'position: fixed',
      'bottom: 24px',
      'left: 50%',
      'transform: translateX(-50%)',
      'z-index: 99999',
      'background: rgba(15, 23, 42, 0.85)',
      'color: #fff',
      'padding: 10px 18px',
      'border-radius: 999px',
      'font-family: system-ui, -apple-system, sans-serif',
      'font-size: 14px',
      'pointer-events: none',
      'box-shadow: 0 8px 30px rgba(0,0,0,0.2)',
    ].join(';')
    document.body.appendChild(el)
  }, message)
  await sleep(durationMs)
}

async function run() {
  await rm(outputDirectory, { recursive: true, force: true })
  await mkdir(outputDirectory, { recursive: true })

  const browser = await chromium.launch({
    headless: false,
    slowMo: 80,
    args: ['--window-size=1440,900'],
  })

  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: {
      dir: outputDirectory,
      size: { width: 1440, height: 900 },
    },
  })

  const page = await context.newPage()

  try {
    // 1. Landing page
    await page.goto(`${baseURL}/`, { waitUntil: 'networkidle' })
    await page.getByRole('main').waitFor({ state: 'visible' })
    await narrate(page, 'Zata Codes Template — 快速启动的全栈项目模板')

    // 2. Public pages
    await page.goto(`${baseURL}/about`, { waitUntil: 'networkidle' })
    await page.getByRole('main').waitFor({ state: 'visible' })
    await sleep(800)
    await narrate(page, '公开页面：About')

    await page.goto(`${baseURL}/features`, { waitUntil: 'networkidle' })
    await page.getByRole('main').waitFor({ state: 'visible' })
    await sleep(800)
    await narrate(page, '公开页面：Features')

    await page.goto(`${baseURL}/pricing`, { waitUntil: 'networkidle' })
    await page.getByRole('main').waitFor({ state: 'visible' })
    await sleep(800)
    await narrate(page, '公开页面：Pricing')

    // 3. Login page
    await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' })
    await page.getByTestId('login-identifier-input').waitFor({ state: 'visible' })
    await narrate(page, '登录页：使用 data-testid 稳定定位表单元素')

    if (shouldLogin) {
      await page.getByTestId('login-identifier-input').fill(credentials.identifier)
      await page.getByTestId('login-password-input').fill(credentials.password)
      await page.getByTestId('login-submit-button').click()

      try {
        await page.waitForURL(`${baseURL}/dashboard`, { timeout: 10_000 })
        await page.getByRole('main').waitFor({ state: 'visible' })
        await narrate(page, '登录成功，进入 Dashboard')

        await page.goto(`${baseURL}/projects`, { waitUntil: 'networkidle' })
        await page.getByRole('main').waitFor({ state: 'visible' })
        await sleep(800)
        await narrate(page, '业务页面：Projects')

        await page.goto(`${baseURL}/tasks`, { waitUntil: 'networkidle' })
        await page.getByRole('main').waitFor({ state: 'visible' })
        await sleep(800)
        await narrate(page, '业务页面：Tasks')
      } catch {
        await narrate(page, '后端未启动，跳过登录后流程', 2_000)
      }
    } else {
      await narrate(page, '设置 DEMO_IDENTIFIER 和 DEMO_PASSWORD 可演示登录后页面', 2_000)
    }

    // End
    await page.goto(`${baseURL}/`, { waitUntil: 'networkidle' })
    await narrate(page, 'Zata Codes Template — 开箱即用的全栈开发起点', 3_000)
  } finally {
    const video = page.video()
    const recordedVideoPath = video ? await video.path() : undefined
    await context.close()
    await browser.close()

    if (recordedVideoPath) {
      const videoPath = resolve(outputDirectory, 'demo-template.webm')
      await rename(recordedVideoPath, videoPath)
      console.log(`Demo video saved to: ${videoPath}`)
    } else {
      console.log('No video was recorded.')
    }
  }
}

run().catch((error) => {
  console.error('Demo recording failed:', error)
  process.exit(1)
})
