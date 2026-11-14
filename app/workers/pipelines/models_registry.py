"""ML model registry: loads models ONCE per worker process
(worker_process_init).

ML_STUB=1 swaps in a fixed-delay fake returning deterministic labels.
Verify the real model APIs against the installed library versions — never
guess. Phase 7. May depend on: core.config.
"""
