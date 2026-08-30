import { expect, test } from '@playwright/test'

test('app shell loads', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByTestId('app-shell')).toBeVisible()
  await expect(page.getByTestId('home-button')).toBeVisible()
  await expect(page.getByTestId('staleness-badge')).toBeVisible()
})
