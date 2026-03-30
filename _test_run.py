"""Full test run — bypasses the random skip-day check so it always runs."""
import main

# Override the skip-day logic so the bot always runs during testing
main._should_skip_today = lambda: (False, "")
main.run_pipeline()
