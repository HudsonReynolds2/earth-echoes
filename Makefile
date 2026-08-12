# Single gate entry point (rule R0). Windows-native shells run ./gate.ps1 instead.
.PHONY: gate testpool-down
gate:
	@sh ./gate.sh

# Close the warm test container pool now (INFRA.1). The pool closes itself when
# a later run finds it idle, so this is for reclaiming RAM early or clearing a
# server that has wedged; it is not part of any gate.
testpool-down:
	@cd backend && uv run python -m tests.testpool --down
