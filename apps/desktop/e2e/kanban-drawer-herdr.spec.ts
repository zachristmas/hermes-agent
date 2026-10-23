/**
 * E2E: the task drawer's linked-Herdr-pane section (kanban-drawer-herdr-pane).
 *
 * The desktop kanban plugin renders a `herdr`-type task_ref as a drawer
 * section: the five-segment pane path (host/session/workspace/tab/pane) plus
 * the direct-Herdr bridge's note (agent, run fence, native-session state).
 * A task without a herdr ref renders no section, and non-herdr ref kinds
 * never appear. The renderer logic is unit-covered in
 * src/plugins/kanban/drawer.test.tsx; this spec exercises the real chain:
 *
 *   electron → hermes serve (source checkout) → kanban plugin API
 *   (/tasks/{id}/refs) → seeded board data
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 *
 * The sandbox HERMES_HOME has no provisioned Python environment, so backend
 * resolution would otherwise fall to the first-run "Set up Hermes Desktop"
 * choice screen. Set HERMES_DESKTOP_PYTHON to an interpreter with the
 * checkout's dependencies (the devshell's activated python) to pin the
 * source-checkout rung — see electron/source-python.ts.
 *
 * Run:
 *   HERMES_DESKTOP_PYTHON="$(python -c 'import sys; print(sys.executable)')" \
 *     npm exec playwright test e2e/kanban-drawer-herdr.spec.ts --reporter=list
 */

import { execFile } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { promisify } from 'node:util'

import { expect, type Page, test } from '@playwright/test'

import { waitForChatReady } from '../../../tests-js/scripts/desktop-chat-smoke'
import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { type MockServerOptions, startMockServer } from '../../../tests-js/scripts/mock-server'

import {
  buildAppEnv,
  createSandbox,
  launchDesktop,
  type Sandbox
} from './fixtures'

const execFileAsync = promisify(execFile)

const DESKTOP_ROOT = path.resolve(import.meta.dirname, '..')
const REPO_ROOT = path.resolve(DESKTOP_ROOT, '..', '..')

/** The pane path the direct-Herdr bridge writes: host/session/workspace/tab/pane. */
const PANE_REF = 'e2e-mac/default/wE2E/wE2E:t1/wE2E:p1'
const PANE_NOTE = 'agent playwright; native session abc123ef; run t_e2e/run-1'
const TASK_WITH_REF = 'E2E drawer herdr pane card'
const TASK_WITHOUT_REF = 'E2E drawer card without a herdr pane'

let python: string | null = null

function resolveSourcePython(): string {
  // Same ladder as the app (electron/source-python.ts): explicit override,
  // then a venv inside the checkout. Fail with the reason, not a mystery
  // first-run overlay 120 seconds in.
  const override = process.env.HERMES_DESKTOP_PYTHON

  if (override && fs.existsSync(override)) {
    return override
  }

  const inRoot = path.join(REPO_ROOT, '.venv', 'bin', 'python')

  if (fs.existsSync(inRoot)) {
    return inRoot
  }

  throw new Error(
    'No source Python found. Set HERMES_DESKTOP_PYTHON to an interpreter with '
    + `the ${REPO_ROOT} dependencies (e.g. the activated devshell python), or create `
    + `${inRoot}. Without it the app cannot start its backend in the e2e sandbox.`
  )
}

/** Seed the sandbox board through the real CLI: two tasks, one with the herdr ref. */
async function seedKanban(sandbox: Sandbox): Promise<void> {
  const env = { HOME: sandbox.root, HERMES_HOME: sandbox.hermesHome, PATH: process.env.PATH ?? '' }

  const run = (args: string[]) => execFileAsync(python!, ['-m', 'hermes_cli.main', ...args], {
    cwd: REPO_ROOT,
    env,
    timeout: 120_000
  })

  const created = await run(['kanban', 'create', TASK_WITH_REF])
  const taskId = /Created (t_\S+)/.exec(created.stdout)?.[1]
  expect(taskId, `kanban create printed a task id: ${created.stdout}`).toBeTruthy()

  await run([
    'kanban', 'ref', 'add', taskId!,
    '--type', 'herdr',
    '--ref', PANE_REF,
    '--host', 'e2e-mac',
    '--note', PANE_NOTE
  ])

  const plain = await run(['kanban', 'create', TASK_WITHOUT_REF])
  expect(/Created (t_\S+)/.exec(plain.stdout)?.[1]).toBeTruthy()
}

/**
 * setupMockBackend with two additions the Herdr section needs: kanban data
 * seeded before launch, and the source python pinned so the app starts its
 * backend instead of the first-run setup choice.
 */
async function setupMockBackendWithKanban(options: { mockServer?: MockServerOptions } = {}) {
  python = resolveSourcePython()
  const mock = await startMockServer(options.mockServer)
  const sandbox = createSandbox('kanban-herdr')
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)
  await seedKanban(sandbox)

  const env = buildAppEnv(sandbox, { HERMES_DESKTOP_PYTHON: python })
  const { app, page } = await launchDesktop(env)

  // The kanban plugin ships opt-in (defaultEnabled: false) and the decision
  // lives in localStorage, read once at renderer boot: seed it and reload so
  // the plugin registers (sidebar entry, /kanban route, statusbar count).
  await page.evaluate(() => {
    window.localStorage.setItem('hermes.desktop.pluginDecisions.v2', JSON.stringify({ kanban: true }))
  })
  await page.reload()

  return {
    app,
    page,
    mock,
    sandbox,
    cleanup: async () => {
      await app.close().catch(() => undefined)
      await mock.close()
      sandbox.cleanup()
    }
  }
}

async function openDrawer(page: Page, title: string): Promise<void> {
  // A drawer left open by an earlier test in this worker owns the screen — the
  // shared Dialog closes on Esc. Harmless when nothing is open.
  await page.keyboard.press('Escape').catch(() => undefined)

  const card = page.getByText(title, { exact: true }).first()

  if (!(await card.isVisible().catch(() => false))) {
    await page.getByRole('button', { name: 'Kanban' }).first().click()
  }

  await card.waitFor({ state: 'visible', timeout: 60_000 })
  await card.click()
}

let fixture: Awaited<ReturnType<typeof setupMockBackendWithKanban>> | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackendWithKanban()
  // The board queries ride the gateway connection; wait for the app shell to
  // be live the same way every other mock-backend spec does.
  await waitForChatReady(fixture.page)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.describe('drawer Herdr pane section', () => {
  test('renders the linked pane from its task_ref through the real backend', async () => {
    const { page } = fixture!
    await openDrawer(page, TASK_WITH_REF)

    const section = page.getByTestId('kanban-herdr-panes')
    await expect(section).toBeVisible()

    // The ref row: row id + the exact pane path `hermes kanban ref list` prints.
    await expect(page.getByTestId('kanban-herdr-ref')).toContainText('#1')
    await expect(page.getByTestId('kanban-herdr-ref')).toContainText(PANE_REF)

    // The five path segments, each on its labeled field.
    await expect(page.getByTestId('kanban-herdr-host')).toContainText('e2e-mac')
    await expect(page.getByTestId('kanban-herdr-session')).toContainText('default')
    await expect(page.getByTestId('kanban-herdr-workspace')).toContainText('wE2E')
    await expect(page.getByTestId('kanban-herdr-tab')).toContainText('wE2E:t1')
    await expect(page.getByTestId('kanban-herdr-pane-id')).toContainText('wE2E:p1')

    // The bridge note: agent, run fence, and the reported native session.
    await expect(page.getByTestId('kanban-herdr-agent')).toContainText('playwright')
    await expect(page.getByTestId('kanban-herdr-run')).toContainText('t_e2e/run-1')
    await expect(page.getByTestId('kanban-herdr-native')).toContainText('reported')
  })

  test('a card without a herdr ref renders no section', async () => {
    const { page } = fixture!
    await openDrawer(page, TASK_WITHOUT_REF)

    // The drawer opened (the card's title is its heading); no Herdr section.
    await expect(page.getByRole('heading', { name: TASK_WITHOUT_REF })).toBeVisible()
    await expect(page.getByTestId('kanban-herdr-panes')).toHaveCount(0)
  })
})
