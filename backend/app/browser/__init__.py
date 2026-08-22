"""Everything that drives a real web browser, in one place.

Read README.md in this directory first — it is written for someone (or
something) reviewing this layer cold, and it explains the design
decisions and the known gap before you read any code.

Nothing is re-exported here on purpose. Import the module you want:

    from backend.app.browser import observation, policy, primitives
    from backend.app.browser.session_manager import get_manager

A convenience surface would hide which of the five modules a caller
actually depends on, and the dependency direction is the thing a reviewer
most needs to see: session_manager owns the Playwright objects, policy
answers "is this allowed", observation answers "what is on the page", and
primitives is the only one the model can reach.
"""
