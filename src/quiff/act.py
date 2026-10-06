"""Stage 4, Act: perform the chosen action.

Prefer accessibility actions (Invoke, SetValue) on Windows and DevTools input in the
browser; real mouse and keyboard input is the fallback. Anything irreversible
(send, pay, delete, submit) asks the user first.
"""
