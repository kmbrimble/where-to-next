# data-testid contract

Playwright tests select elements by `data-testid`, not CSS classes or text, so UI copy and
styling can change without breaking tests. This is the current contract — add to it as UI
grows, don't invent new ids ad hoc from inside a test file.

| data-testid | Element | Notes |
|---|---|---|
| `app-shell` | Root container | Present once the app has mounted. |
| `home-button` | Home button (sticky, top-left) | Opens the day-navigation menu. |
| `staleness-badge` | "Data as of HH:MM" indicator | Reflects `fetchedAt` from `src/lib/api.ts`. |
| `main-content` | Main content region | Itinerary/day views render inside this. |
