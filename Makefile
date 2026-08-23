.PHONY: help install install-dev test lint gui server seedkey-server simulator demo clean

PY ?= python3

help:
	@echo "Targets:"
	@echo "  install        install the package"
	@echo "  install-dev    install with dev + optional extras"
	@echo "  test           run the pytest suite"
	@echo "  demo           run an end-to-end simulated flash"
	@echo "  gui            launch the desktop GUI"
	@echo "  server         run the firmware file server"
	@echo "  seedkey-server run the seed/key server"
	@echo "  simulator      run a stand-alone virtual MED17.7.5"
	@echo "  clean          remove build/test artifacts"

install:
	$(PY) -m pip install .

install-dev:
	$(PY) -m pip install -e ".[dev,all]"

test:
	$(PY) -m pytest

demo:
	$(PY) scripts/demo_flash.py

gui:
	$(PY) -m med17flasher gui

server:
	$(PY) -m med17flasher fileserver --root ./firmware-repo

seedkey-server:
	$(PY) -m med17flasher seedkey-server

simulator:
	$(PY) -m med17flasher simulator

desktop:
	cd webui && npm ci && npm run build
	$(PY) -m pip install pyinstaller
	$(PY) -m PyInstaller --clean --noconfirm packaging/med17flasher.spec
	@echo "built: dist/med17flasher-desktop"

clean:
	rm -rf build dist *.egg-info .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
