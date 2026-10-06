"""The investment committee.

Twice a day (and on demand) the scheduler holds a meeting:

  collector.build()   ->  a compact, numeric briefing from the live system
  analyst.analyse()   ->  the minutes, written by the LLM against the policy
  runner.hold_meeting ->  persist, apply what the policy allows, notify

The committee never places, closes or resizes a position. Its only automatic
powers are the ones the advisor already has (a market stance and a risk
multiplier for NEW entries); everything else is a proposal the user
approves or declines on the panel (app/api/routers/committee.py).
"""
