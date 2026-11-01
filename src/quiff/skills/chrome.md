# Chrome

How Chrome exposes itself and how to drive it.

## Structure

- The page becomes `link`, `button`, `textbox`, `checkbox`, `combobox`,
  `heading`, and other rows — the numbered action table covers the CURRENT
  VIEWPORT only. Press PageDown (then re-observe) to reach content below
  the fold; the header line reports how many controls were filtered.
- The browser's own UI (address bar, tabs, bookmarks) is NOT part of the
  table and cannot be driven by page actions.

## Flows

- **Go to a URL**: use the navigate action with the full URL. Never try
  Ctrl+L or the address bar — it is browser chrome, unreachable from the
  page. Composing a search-query URL (e.g. `https://site/search?q=terms`)
  is often faster than using the site's search box.
- **Type into a field**: the type action focuses the field, selects its
  content, and replaces it. The engine verifies the value landed.
- **Pick from a dropdown**: use select with the option's visible text.
- **Submit a form**: click the submit button; pressing Enter works only
  when a field holds focus.
- **Download a file**: links to text/plain or inline-served files DISPLAY
  in the page instead of downloading — use the download action with the
  file's URL and the destination path.

## Quirks

- A page can take seconds to become interactive: wait only while an
  action is clearly in progress, then re-observe.
- Some sites block automated clients (Google serves a `/sorry` page);
  navigate away or use a different route rather than retrying.
- Popups and new tabs from `target=_blank` links open outside the run's
  tab; prefer links that stay in the current tab.
