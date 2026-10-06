"""Stage 2, Recall: replay known flows with no model call.

Hash the screen's structure together with the current subgoal. If a recorded flow
matches, replay its next action, checking the screen before each action.
"""
