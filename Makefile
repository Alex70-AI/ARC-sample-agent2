.PHONY: sync run task

sync:
	uv sync

run:
	uv run python main.py

task:
	@if [ -z "$(SPEC)" ]; then echo "usage: make task SPEC=notification_raise"; exit 1; fi
	uv run python main.py --spec $(SPEC)
