.PHONY: test compile smoke proof

test:
	pytest -q

compile:
	python -m compileall -q src

smoke:
	PYTHONPATH=src python -m sif.cli --cycles 1 --json

proof:
	python scripts/build_proof_pack.py
