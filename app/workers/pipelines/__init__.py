"""Per-kind processing pipelines run by workers.tasks.process_file (Phase 7).

Each pipeline replaces the file's file_labels in one transaction.
May depend on: repositories.labels, models_registry.
"""
