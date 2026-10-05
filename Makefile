.PHONY: install install-hardware test simulate pvs docker-build docker-sim

install:
	python3 -m pip install -e .

install-hardware:
	python3 -m pip install -e '.[hardware]'

test:
	python3 -m pytest

simulate:
	thorlabscct10-ioc --simulate --prefix CCT10:

pvs:
	thorlabscct10-ioc --simulate --prefix CCT10: --list-pvs

docker-build:
	docker build -t thorlabscct10-ioc:latest .

docker-sim:
	docker compose -f compose.simulator.yaml up --build
