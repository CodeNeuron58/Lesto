# File Explorer

How Windows Explorer exposes itself and how to drive it.

## Structure

- Files and folders appear as `list item` rows named by their filename,
  inside a `list` container. The address box and toolbar buttons
  (New, Sort, View, ...) are separate rows.
- One Explorer process hosts every window; Quiff only touches the window
  it launched. Ignore any other Explorer window on screen.

## Flows

- **Create a folder**: press Ctrl+Shift+N — a new folder appears with its
  name selected for inline rename → type the name → press Enter.
  (The New button in the toolbar does the same.)
- **Rename a file**: click the file to select it → press F2 → the inline
  rename edit activates → type the new name → press Enter.
- **Delete**: select the item → press Delete → confirm if asked.
- **Open a folder**: click its `list item` row (or press Enter on it).

## Quirks

- Explorer may HIDE file extensions: the rename box for `draft.txt` shows
  only `draft`, and typing `final.txt` can produce `final.txt.txt`.
  After a rename, check the resulting name; if it doubled, rename again
  typing only the base name.
- The inline rename edit auto-selects the current name — typing replaces
  it. Escape cancels a rename.
- Files may be listed with their type column visible; the row name is the
  file name.
